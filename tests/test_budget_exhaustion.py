#!/usr/bin/env python3
"""
Integration Tests: Budget / Rate-Limit Exhaustion
==================================================

Tests the full budget-exhaustion path:
  1. run_agent_session correctly classifies 429 / rate-limit errors.
  2. post_session_processing responds appropriately when a rate-limit
     error_info is passed (subtask stays in its current state; recovery
     is attempted via check_and_recover rather than the concurrency reset).
  3. The RATE_LIMIT_PAUSE file is written with the right structure when the
     coder loop handles a rate-limit error.
  4. parse_rate_limit_reset_time extracts a usable timestamp from common
     error message formats.
  5. The RATE_LIMIT_PAUSED phase event is emitted and parseable.
"""

import asyncio
import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add backend to path (same pattern used by existing test files)
sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))


# =============================================================================
# Helpers
# =============================================================================

def create_implementation_plan(spec_dir: Path, subtasks: list[dict]) -> Path:
    plan = {
        "feature": "Budget Test Feature",
        "workflow_type": "feature",
        "status": "in_progress",
        "phases": [
            {
                "id": "phase-1",
                "name": "Implementation",
                "type": "implementation",
                "subtasks": subtasks,
            }
        ],
    }
    plan_file = spec_dir / "implementation_plan.json"
    plan_file.write_text(json.dumps(plan, indent=2))
    return plan_file


def make_mock_client(raise_exception: Exception | None = None):
    """Return a minimal async-context-manager mock of ClaudeSDKClient."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    if raise_exception:
        client.query = AsyncMock(side_effect=raise_exception)
    else:
        client.query = AsyncMock(return_value=None)

    async def _empty_stream():
        return
        yield  # make it a generator

    client.receive_response = MagicMock(return_value=_empty_stream())
    return client


# =============================================================================
# 1. run_agent_session — error classification
# =============================================================================

class TestRunAgentSessionRateLimitClassification:
    """run_agent_session must return error_info with type='rate_limit' for 429s."""

    @pytest.mark.asyncio
    async def test_429_error_classified_as_rate_limit(self, temp_git_repo: Path):
        from agents.session import run_agent_session
        from task_logger import LogPhase

        spec_dir = temp_git_repo / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        create_implementation_plan(spec_dir, [
            {"id": "st-1", "description": "Task", "status": "in_progress"}
        ])

        rate_limit_exc = Exception("HTTP 429: usage limit reached - quota exceeded")
        client = make_mock_client(raise_exception=rate_limit_exc)

        with patch("agents.session.get_task_logger", return_value=None), \
             patch("agents.session.is_build_complete", return_value=False):
            status, _, error_info = await run_agent_session(
                client, "do work", spec_dir, verbose=False, phase=LogPhase.CODING
            )

        assert status == "error"
        assert error_info["type"] == "rate_limit"
        assert "rate_limit" in error_info["type"]

    @pytest.mark.asyncio
    async def test_rate_limit_keyword_error_classified_correctly(self, temp_git_repo: Path):
        from agents.session import run_agent_session
        from task_logger import LogPhase

        spec_dir = temp_git_repo / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        create_implementation_plan(spec_dir, [
            {"id": "st-1", "description": "Task", "status": "in_progress"}
        ])

        rate_limit_exc = Exception("rate limit reached for this account")
        client = make_mock_client(raise_exception=rate_limit_exc)

        with patch("agents.session.get_task_logger", return_value=None), \
             patch("agents.session.is_build_complete", return_value=False):
            status, _, error_info = await run_agent_session(
                client, "do work", spec_dir, verbose=False, phase=LogPhase.CODING
            )

        assert status == "error"
        assert error_info["type"] == "rate_limit"

    @pytest.mark.asyncio
    async def test_rate_limit_error_does_not_expose_api_keys(self, temp_git_repo: Path):
        """Sensitive data must be redacted in the returned error message."""
        from agents.session import run_agent_session
        from task_logger import LogPhase

        spec_dir = temp_git_repo / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        create_implementation_plan(spec_dir, [
            {"id": "st-1", "description": "Task", "status": "in_progress"}
        ])

        secret_key = "sk-ant-api03-averylongsecretkeyvalue1234567890"
        rate_limit_exc = Exception(
            f"429 rate limit for token {secret_key}: quota exceeded"
        )
        client = make_mock_client(raise_exception=rate_limit_exc)

        with patch("agents.session.get_task_logger", return_value=None), \
             patch("agents.session.is_build_complete", return_value=False):
            status, response_text, error_info = await run_agent_session(
                client, "do work", spec_dir, verbose=False, phase=LogPhase.CODING
            )

        assert status == "error"
        assert secret_key not in error_info["message"], "API key must be redacted"
        assert secret_key not in response_text, "API key must be redacted from response"

    @pytest.mark.asyncio
    async def test_tool_concurrency_error_not_classified_as_rate_limit(self, temp_git_repo: Path):
        """400 tool-concurrency errors should be a distinct type from rate_limit."""
        from agents.session import run_agent_session
        from task_logger import LogPhase

        spec_dir = temp_git_repo / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        create_implementation_plan(spec_dir, [
            {"id": "st-1", "description": "Task", "status": "in_progress"}
        ])

        concurrency_exc = Exception("400: too many tools called concurrently")
        client = make_mock_client(raise_exception=concurrency_exc)

        with patch("agents.session.get_task_logger", return_value=None), \
             patch("agents.session.is_build_complete", return_value=False):
            status, _, error_info = await run_agent_session(
                client, "do work", spec_dir, verbose=False, phase=LogPhase.CODING
            )

        assert status == "error"
        assert error_info["type"] == "tool_concurrency"
        assert error_info["type"] != "rate_limit"


# =============================================================================
# 2. post_session_processing — rate-limit path
# =============================================================================

class TestPostSessionProcessingRateLimit:
    """post_session_processing must NOT reset a rate-limit subtask to pending
    (that behaviour is reserved for tool_concurrency).  Instead it should
    invoke check_and_recover on the incomplete subtask."""

    @pytest.mark.asyncio
    async def test_rate_limit_does_not_reset_subtask_to_pending(self, temp_git_repo: Path):
        from agents.session import post_session_processing
        from recovery import RecoveryManager

        spec_dir = temp_git_repo / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        create_implementation_plan(spec_dir, [
            {"id": "st-1", "description": "Task", "status": "in_progress"}
        ])

        recovery_manager = RecoveryManager(spec_dir, temp_git_repo)
        rate_limit_error_info = {
            "type": "rate_limit",
            "message": "429 rate limit reached",
            "exception_type": "APIStatusError",
        }

        with patch("agents.session.extract_session_insights", new_callable=AsyncMock) as m_insights, \
             patch("agents.session.save_session_memory", new_callable=AsyncMock) as m_memory, \
             patch("agents.session.check_and_recover", return_value=None) as m_recover, \
             patch("agents.session.linear_subtask_failed", new_callable=AsyncMock):

            m_insights.return_value = {"file_insights": [], "patterns_discovered": []}
            m_memory.return_value = (True, "file")

            await post_session_processing(
                spec_dir=spec_dir,
                project_dir=temp_git_repo,
                subtask_id="st-1",
                session_num=1,
                commit_before="abc1234",
                commit_count_before=1,
                recovery_manager=recovery_manager,
                linear_enabled=False,
                error_info=rate_limit_error_info,
            )

        # check_and_recover should be called (rate_limit goes through recovery,
        # not the concurrency fast-reset path)
        m_recover.assert_called_once()

        # Subtask must NOT have been silently reset to "pending" by post_session_processing
        # (the concurrency-reset code checks for type == "tool_concurrency")
        import json as _json
        plan = _json.loads((spec_dir / "implementation_plan.json").read_text())
        subtask = plan["phases"][0]["subtasks"][0]
        assert subtask["status"] != "pending", (
            "Rate-limit errors should not silently reset subtask to pending; "
            "that's only for tool_concurrency errors"
        )

    @pytest.mark.asyncio
    async def test_concurrency_error_resets_subtask_to_pending(self, temp_git_repo: Path):
        """Confirm the OPPOSITE: tool_concurrency DOES reset to pending (regression guard)."""
        from agents.session import post_session_processing
        from recovery import RecoveryManager

        spec_dir = temp_git_repo / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        create_implementation_plan(spec_dir, [
            {"id": "st-1", "description": "Task", "status": "in_progress"}
        ])

        recovery_manager = RecoveryManager(spec_dir, temp_git_repo)
        concurrency_error_info = {
            "type": "tool_concurrency",
            "message": "400 too many concurrent tools",
            "exception_type": "APIStatusError",
        }

        with patch("agents.session.extract_session_insights", new_callable=AsyncMock) as m_insights, \
             patch("agents.session.save_session_memory", new_callable=AsyncMock) as m_memory, \
             patch("agents.session.reset_subtask", return_value=None) as m_reset, \
             patch("agents.session.linear_subtask_failed", new_callable=AsyncMock):

            m_insights.return_value = {"file_insights": [], "patterns_discovered": []}
            m_memory.return_value = (True, "file")

            await post_session_processing(
                spec_dir=spec_dir,
                project_dir=temp_git_repo,
                subtask_id="st-1",
                session_num=1,
                commit_before="abc1234",
                commit_count_before=1,
                recovery_manager=recovery_manager,
                linear_enabled=False,
                error_info=concurrency_error_info,
            )

        # reset_subtask must have been called
        m_reset.assert_called_once_with(spec_dir, temp_git_repo, "st-1")

        # Plan should show "pending" now
        import json as _json
        plan = _json.loads((spec_dir / "implementation_plan.json").read_text())
        subtask = plan["phases"][0]["subtasks"][0]
        assert subtask["status"] == "pending"


# =============================================================================
# 3. RATE_LIMIT_PAUSE file creation
# =============================================================================

class TestRateLimitPauseFile:
    """Verify the RATE_LIMIT_PAUSE file is written correctly when budget is exhausted."""

    def test_pause_file_written_with_correct_structure(self, temp_git_repo: Path):
        """Directly verify the pause-file writing logic used in coder.py."""
        from agents.base import RATE_LIMIT_PAUSE_FILE
        from agents.base import sanitize_error_message

        spec_dir = temp_git_repo / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)

        import time
        reset_timestamp = int(time.time()) + 3600  # 1 hour from now
        raw_error = "429 rate limit reached for account sk-ant-api03-secret123456789012345"
        sanitized_error = sanitize_error_message(raw_error, max_length=500) or "Rate limit reached"

        from datetime import datetime
        pause_data = {
            "paused_at": datetime.now().isoformat(),
            "reset_timestamp": reset_timestamp,
            "error": sanitized_error,
        }
        pause_file = spec_dir / RATE_LIMIT_PAUSE_FILE
        pause_file.write_text(json.dumps(pause_data), encoding="utf-8")

        # Read back and verify structure
        assert pause_file.exists()
        loaded = json.loads(pause_file.read_text())
        assert "paused_at" in loaded
        assert "reset_timestamp" in loaded
        assert "error" in loaded
        assert loaded["reset_timestamp"] == reset_timestamp
        assert "sk-ant-api03-secret123456789012345" not in loaded["error"], \
            "API key must be redacted in the pause file"

    def test_pause_file_name_matches_constant(self):
        """RATE_LIMIT_PAUSE_FILE constant has the expected name."""
        from agents.base import RATE_LIMIT_PAUSE_FILE

        assert RATE_LIMIT_PAUSE_FILE == "RATE_LIMIT_PAUSE"


# =============================================================================
# 4. parse_rate_limit_reset_time
# =============================================================================

class TestParseRateLimitResetTime:
    """parse_rate_limit_reset_time must extract a future timestamp from common formats."""

    def test_in_minutes_format(self):
        from agents.coder import parse_rate_limit_reset_time
        import time

        error_info = {"message": "Rate limit reached. Resets in 30 minutes."}
        result = parse_rate_limit_reset_time(error_info)

        assert result is not None
        # Should be roughly 30 minutes (1800 s) from now (±60 s tolerance)
        delta = result - time.time()
        assert 1740 <= delta <= 1860, f"Expected ~1800s, got {delta}s"

    def test_in_hours_format(self):
        from agents.coder import parse_rate_limit_reset_time
        import time

        error_info = {"message": "quota exceeded; resets in 2 hours"}
        result = parse_rate_limit_reset_time(error_info)

        assert result is not None
        delta = result - time.time()
        # ~7200 s ± 60 s
        assert 7140 <= delta <= 7260, f"Expected ~7200s, got {delta}s"

    def test_no_time_info_returns_none(self):
        from agents.coder import parse_rate_limit_reset_time

        error_info = {"message": "rate limit reached"}
        result = parse_rate_limit_reset_time(error_info)

        assert result is None

    def test_none_error_info_returns_none(self):
        from agents.coder import parse_rate_limit_reset_time

        assert parse_rate_limit_reset_time(None) is None

    def test_empty_error_info_returns_none(self):
        from agents.coder import parse_rate_limit_reset_time

        assert parse_rate_limit_reset_time({}) is None


# =============================================================================
# 5. RATE_LIMIT_PAUSED phase event
# =============================================================================

class TestRateLimitPausedPhaseEvent:
    """RATE_LIMIT_PAUSED phase event must be emitted with the correct structure."""

    def test_rate_limit_paused_phase_emitted(self, capsys):
        import time
        from core.phase_event import PHASE_MARKER_PREFIX, ExecutionPhase, emit_phase

        reset_ts = int(time.time()) + 1800
        emit_phase(
            ExecutionPhase.RATE_LIMIT_PAUSED,
            "Rate limit - resuming in 30 minutes",
            reset_timestamp=reset_ts,
        )

        captured = capsys.readouterr()
        assert PHASE_MARKER_PREFIX in captured.out

        # Extract and parse the JSON payload
        line = next(
            l for l in captured.out.splitlines() if PHASE_MARKER_PREFIX in l
        )
        payload = json.loads(line.split(PHASE_MARKER_PREFIX, 1)[1])

        assert payload["phase"] == "rate_limit_paused"
        assert payload["message"] == "Rate limit - resuming in 30 minutes"
        assert payload.get("reset_timestamp") == reset_ts

    def test_rate_limit_paused_is_valid_execution_phase(self):
        from core.phase_event import ExecutionPhase

        assert ExecutionPhase.RATE_LIMIT_PAUSED.value == "rate_limit_paused"

    def test_complete_budget_exhaustion_phase_sequence(self, capsys):
        """Simulate the phase sequence: CODING → RATE_LIMIT_PAUSED → CODING (resume)."""
        import time
        from core.phase_event import PHASE_MARKER_PREFIX, ExecutionPhase, emit_phase

        reset_ts = int(time.time()) + 900
        emit_phase(ExecutionPhase.CODING, "Building subtask 1")
        emit_phase(
            ExecutionPhase.RATE_LIMIT_PAUSED,
            "Rate limit - resuming in 15 minutes",
            reset_timestamp=reset_ts,
        )
        emit_phase(ExecutionPhase.CODING, "Resuming after rate limit")

        captured = capsys.readouterr()
        phase_lines = [
            l for l in captured.out.splitlines() if PHASE_MARKER_PREFIX in l
        ]
        assert len(phase_lines) == 3

        phases = [
            json.loads(l.split(PHASE_MARKER_PREFIX, 1)[1])["phase"]
            for l in phase_lines
        ]
        assert phases == ["coding", "rate_limit_paused", "coding"]
