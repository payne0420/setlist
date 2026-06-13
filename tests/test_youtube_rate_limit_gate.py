"""Unit tests for the YouTube session rate-limit gate (Qt-free)."""

import threading
import time

import pytest

from Spotify_Downloader import _YouTubeRateLimitGate


@pytest.fixture
def cancel_event():
    return threading.Event()


@pytest.fixture
def emit_log():
    return []


@pytest.fixture
def gate(cancel_event, emit_log):
    return _YouTubeRateLimitGate(cancel_event, emit_log.append)


class TestYouTubeRateLimitGate:
    def test_fresh_gate_returns_go_immediately(self, gate):
        assert gate.before_attempt() == "GO"

    def test_after_rate_limit_blocks_then_returns_go(self, gate):
        gate.COOLDOWN_S = 0.05
        gate.SLICE_S = 0.01
        gate.after_rate_limit()
        t0 = time.monotonic()
        assert gate.before_attempt() == "GO"
        assert time.monotonic() - t0 >= 0.04

    def test_second_rate_limit_in_window_does_not_extend_deadline(self, gate):
        gate.COOLDOWN_S = 10.0
        gate.after_rate_limit()
        first_until = gate._until
        gate.after_rate_limit()
        assert gate._until == first_until

    def test_after_clear_resets_state_and_rearms_notice(self, gate, emit_log):
        gate.COOLDOWN_S = 10.0
        gate.after_rate_limit()
        gate._notice_sent = True
        gate.after_clear()
        assert gate._until == 0.0
        assert gate._notice_sent is False
        assert gate._engaged_at is None
        assert "restored" in emit_log[-1]

        emit_log.clear()
        gate.after_rate_limit()
        gate.before_attempt()
        assert len(emit_log) == 1
        assert "holding downloads" in emit_log[0]

    def test_cancel_returns_cancel_promptly(self, gate, cancel_event):
        gate.COOLDOWN_S = 10.0
        gate.after_rate_limit()
        cancel_event.set()
        t0 = time.monotonic()
        assert gate.before_attempt() == "CANCEL"
        assert time.monotonic() - t0 < 1.0

    def test_cancel_returns_cancel_mid_pause(self, gate, cancel_event):
        gate.COOLDOWN_S = 5.0
        gate.SLICE_S = 0.02
        gate.after_rate_limit()
        result = None

        def waiter():
            nonlocal result
            result = gate.before_attempt()

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.1)
        cancel_event.set()
        t0 = time.monotonic()
        thread.join(timeout=2.0)
        assert thread.is_alive() is False
        assert result == "CANCEL"
        assert time.monotonic() - t0 < 1.0

    def test_max_hold_cap_returns_go_while_still_paused(self, gate):
        gate.COOLDOWN_S = 10.0
        gate.after_rate_limit()
        gate._engaged_at = time.monotonic() - gate.MAX_HOLD_S - 1
        assert gate._until > time.monotonic()
        assert gate.before_attempt() == "GO"

    def test_pause_notice_emitted_once_across_polls(self, gate, emit_log):
        gate.COOLDOWN_S = 0.08
        gate.SLICE_S = 0.02
        gate.after_rate_limit()

        result = None

        def waiter():
            nonlocal result
            result = gate.before_attempt()

        thread = threading.Thread(target=waiter)
        thread.start()
        thread.join(timeout=2.0)
        assert result == "GO"
        pause_msgs = [m for m in emit_log if "holding downloads" in m]
        assert len(pause_msgs) == 1
