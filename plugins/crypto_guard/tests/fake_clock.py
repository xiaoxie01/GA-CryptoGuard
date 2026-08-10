# -*- coding: utf-8 -*-
"""5.1 module-level fake clock for time-dependent tests (test-side only).

Rebinds ONLY the module-under-test's ``time`` reference, e.g.
``patch.object(llm_agent_judge, "time", FakeClock())``. The global ``time``
module is NEVER patched, so every other module keeps real time — the injection
is strictly scoped to the one module-under-test whose sleeps are being removed.

All attributes except ``sleep`` delegate to the real ``time`` module (via
``__getattr__``), so ``perf_counter()`` / ``monotonic()`` / ``time()`` used for
latency or staleness measurement still return REAL elapsed time. Only
``sleep()`` is intercepted: it records the requested duration (so a test can
assert the sleep branch actually ran) and returns immediately without
blocking wall-clock.

This is a TEST-TIME mechanism only. Production sleeps (subprocess reaping,
advisory-lock hold windows, barrier rendezvous, slow-tool sim, retry jitter in
the running service) stay real — the injected clock exists to prove and cover
sleep paths deterministically WITHOUT burning wall-clock in the suite.
"""

import time as _real_time


class FakeClock:
    """Drop-in ``time`` substitute: real clock, ``sleep`` is a no-op."""

    def __init__(self) -> None:
        #: Every ``sleep(seconds)`` request, in call order (assertable).
        self.sleep_calls: list[float] = []

    def sleep(self, seconds: float) -> None:
        """Record the sleep; do NOT actually block (drops wall-clock)."""
        self.sleep_calls.append(float(seconds))

    def __getattr__(self, name: str):
        """Delegate every other ``time.*`` attribute to the real module."""
        return getattr(_real_time, name)
