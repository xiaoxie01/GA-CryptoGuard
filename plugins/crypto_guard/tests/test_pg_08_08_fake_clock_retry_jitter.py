# -*- coding: utf-8 -*-
"""5.1 RED-first: module-level fake clock drops the retry-jitter wall-clock.

Scope finding (recorded in research/step5-1.md): the collected suite's real
sleeps are all semantically required (slow-tool sim, advisory-lock hold window,
barrier rendezvous proof, subprocess reaping). The ONE test-reachable
production sleep never exercised by the collected suite is the retry jitter in
``llm_agent_judge._call_ga_llm_with_retry`` (llm_agent_judge.py:1362-1368) — a
real 2-20s ``time.sleep`` between retry attempts.

This test drives the REAL retry wrapper to attempt 2+ by patching
``_call_ga_llm`` to return invalid JSON on every call (attempt 1 ->
``llm_json_parse_failed``, retryable -> jitter sleep on attempts 2/3), then:

* asserts the jitter branch actually executed — ``FakeClock.sleep_calls`` is
  non-empty (branch coverage that was previously 0%);
* asserts the wrapper completed within 1.0s wall-clock;
* injects ``FakeClock`` via ``patch.object(llm_agent_judge, "time", ...)`` —
  module-level injection; the global ``time`` module is never patched.

The prompt builders are stubbed to a trivial string: ``snapshot``/``fallback``
are consumed ONLY by the prompt builder (``_run_single_llm_attempt`` uses them
nowhere else), and the wrapper's admission gates / retry loop / jitter sleep /
breaker events / attempt_meta accumulation are the behavior under test. Prompt
content is irrelevant to the jitter branch.

RED (mechanism proof, recorded in step5-1.md): with the injection removed the
same wrapper burns 4-34s of REAL jitter sleeps and the <1.0s bound FAILS.
GREEN: the injected clock drops wall-clock to ~0s and the bound holds.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

from plugins.crypto_guard.reasoning import llm_agent_judge
from plugins.crypto_guard.reasoning.llm_breaker import CircuitBreaker
from plugins.crypto_guard.tests.fake_clock import FakeClock


class TestFakeClockRetryJitter(unittest.TestCase):
    """5.1: injected module-level clock drops retry-jitter wall-clock."""

    def _harness(self):
        """Build a real retry-wrapper call that reaches attempt 2+.

        ``_call_ga_llm`` returning invalid JSON -> ``llm_json_parse_failed``
        (retryable) on EVERY attempt, so the wrapper runs the full 3-attempt
        loop and the jitter sleeps on attempts 2 and 3.
        """
        breaker = CircuitBreaker(
            enabled=True, consecutive_threshold=3, rate_threshold=0.5,
            rate_window=10, min_rate_samples=5,
        )

        def _trivial_prompt(snapshot, fallback, *, context=None):
            return "{}"  # content irrelevant; _call_ga_llm is patched

        def _invalid_json(prompt):
            return "{not valid json"  # -> llm_json_parse_failed (retryable)

        return breaker, _trivial_prompt, _invalid_json

    @pytest.mark.serial
    def test_retry_jitter_elapsed_is_bounded_by_injected_clock(self) -> None:
        # Serial: the <1.0s bound measures real wall-clock around the wrapper,
        # so sibling xdist workers on this 6-physical-core machine can starve
        # the process past 1.0s even though all sleeps are injected (no-ops).
        # The serial stage runs single-process -> the bound measures only the
        # wrapper under test. Assertion semantics unchanged (R1-4).
        breaker, trivial, invalid = self._harness()
        fake = FakeClock()
        t0 = time.monotonic()
        with patch.object(llm_agent_judge, "_call_ga_llm", side_effect=invalid):
            with patch.object(llm_agent_judge, "time", fake):
                candidate, attempt_meta = llm_agent_judge._call_ga_llm_with_retry(
                    snapshot={},
                    fallback={},
                    context=None,  # legacy admission path (ESTIMATED_CALL_MS + jitter gate)
                    breaker=breaker,
                    prompt_builders=(trivial, trivial, trivial),
                )
        elapsed = time.monotonic() - t0
        self.assertIsNone(candidate, "all attempts failed -> no candidate")
        self.assertGreaterEqual(
            int(attempt_meta["llm_attempt_count"]), 2,
            "invalid JSON must retry to attempt 2+",
        )
        self.assertEqual(
            attempt_meta["llm_terminal_reason"], "llm_json_parse_failed",
        )
        # The jitter sleep branch MUST have executed (previously 0% covered).
        self.assertGreaterEqual(
            len(fake.sleep_calls), 2,
            "retry jitter sleeps for attempts 2/3 must fire",
        )
        # And the injected clock must have dropped their wall-clock.
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
