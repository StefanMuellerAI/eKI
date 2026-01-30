"""Tests for Temporal workflows and activities."""

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from workflows.activities import (
    analyze_risks_activity,
    deliver_report_activity,
    generate_report_activity,
    parse_script_activity,
)
from workflows.security_check import SecurityCheckWorkflow


@pytest.mark.asyncio
class TestActivities:
    """Tests for Temporal activities."""

    async def test_parse_script_activity(self):
        """Test parse_script activity."""
        script_data = {
            "script_content": "VGVzdCBzY3JpcHQ=",  # Base64 "Test script"
            "script_format": "fdx",
            "project_id": "test-123",
        }

        result = await parse_script_activity(script_data)

        assert "scenes" in result
        assert "total_scenes" in result
        assert "parsing_time_seconds" in result
        assert result["metadata"]["stub"] is True

    async def test_analyze_risks_activity(self):
        """Test analyze_risks activity."""
        parsed_data = {
            "scenes": [],
            "total_scenes": 0,
        }

        result = await analyze_risks_activity(parsed_data)

        assert "findings" in result
        assert "risk_summary" in result
        assert "total_findings" in result
        assert result["metadata"]["stub"] is True

    async def test_generate_report_activity(self):
        """Test generate_report activity."""
        analysis_data = {
            "findings": [],
            "risk_summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 1},
            "total_findings": 1,
        }

        job_metadata = {
            "report_id": "123e4567-e89b-12d3-a456-426614174000",
            "project_id": "test-project-123",
        }

        result = await generate_report_activity(analysis_data, job_metadata)

        assert result["report_id"] == job_metadata["report_id"]
        assert result["project_id"] == job_metadata["project_id"]
        assert "findings" in result
        assert result["metadata"]["stub"] is True

    async def test_deliver_report_activity(self):
        """Test deliver_report activity."""
        report = {
            "report_id": "123e4567-e89b-12d3-a456-426614174000",
            "project_id": "test-project-123",
        }

        result = await deliver_report_activity(report)

        assert result["delivered"] is True
        assert "delivery_time_seconds" in result
        assert result["metadata"]["stub"] is True

    async def test_deliver_report_with_callback(self):
        """Test deliver_report activity with callback URL."""
        report = {
            "report_id": "123e4567-e89b-12d3-a456-426614174000",
            "project_id": "test-project-123",
        }

        callback_url = "https://example.com/callback"

        result = await deliver_report_activity(report, callback_url)

        assert result["delivered"] is True
        assert result["callback_sent"] is True


@pytest.mark.asyncio
class TestWorkflows:
    """Tests for Temporal workflows."""

    async def test_security_check_workflow(self):
        """Test SecurityCheckWorkflow execution."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[SecurityCheckWorkflow],
                activities=[
                    parse_script_activity,
                    analyze_risks_activity,
                    generate_report_activity,
                    deliver_report_activity,
                ],
            ):
                job_data = {
                    "script_content": "VGVzdCBzY3JpcHQ=",
                    "script_format": "fdx",
                    "project_id": "test-project-123",
                    "job_id": "job-123",
                    "report_id": "report-456",
                    "user_id": "user-789",
                }

                result = await env.client.execute_workflow(
                    SecurityCheckWorkflow.run,
                    job_data,
                    id="test-workflow-1",
                    task_queue="test-queue",
                )

                assert result["status"] == "completed"
                assert result["delivered"] is True
                assert "report_id" in result
                assert "workflow_id" in result


@pytest.mark.asyncio
class TestWorkflowError:
    """Tests for workflow error handling."""

    async def test_workflow_with_invalid_data(self):
        """Test workflow with invalid input data."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[SecurityCheckWorkflow],
                activities=[
                    parse_script_activity,
                    analyze_risks_activity,
                    generate_report_activity,
                    deliver_report_activity,
                ],
            ):
                job_data = {
                    "script_content": "VGVzdCBzY3JpcHQ=",
                    "script_format": "fdx",
                    "project_id": "test-project-123",
                    # Missing required fields
                }

                result = await env.client.execute_workflow(
                    SecurityCheckWorkflow.run,
                    job_data,
                    id="test-workflow-2",
                    task_queue="test-queue",
                )

                # Workflow should handle errors gracefully
                assert "status" in result
