"""Batch-scoped LLM circuit breaker, retry budget, and wall-clock budget.

Design references: design.md §5.1 (CircuitBreaker), §4.2 (BatchRetryBudget),
§4.3 (BatchWallClockBudget).

The breaker lifetime is one batch. A new batch starts with a fresh breaker
in closed state. The breaker is NOT a global singleton — the controller
creates one per batch and passes it via ``context["llm_breaker"]``.
"""
from __future__ import annotations

import threading
import time
from typing import Any


class CircuitBreaker:
    """Batch-scoped LLM circuit breaker.

    States: closed | open | half_open.
    - closed: normal LLM calls.
    - open: skip LLM, deterministic fallback only.
    - half_open: allow ONE probe call; on success -> closed; on fail -> open.

    Open conditions (checked after each attempt):
    - llm_config_error: open IMMEDIATELY (any count).
    - ``consecutive_threshold`` consecutive llm_transport_error /
      llm_empty_response: open.
    - fail rate >= ``rate_threshold`` over the latest ``rate_window`` LLM
      calls, BUT only when at least ``min_rate_samples`` observations
      exist. Pre-07-09-overtrigger the rate check fired at 3 samples, so
      [fail, fail, success] opened the breaker and skipped the remaining
      7-8 symbols of a 10-symbol batch. With ``min_rate_samples=5`` (the
      default), the same sequence leaves the breaker closed.

    Half-open transition: not automatic within a batch. A new batch starts
    with a fresh breaker in closed state (breaker lifetime == one batch).
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        consecutive_threshold: int = 3,
        rate_threshold: float = 0.5,
        rate_window: int = 10,
        min_rate_samples: int = 5,
    ):
        self._enabled = enabled
        self._consecutive_threshold = consecutive_threshold
        self._rate_threshold = rate_threshold
        self._rate_window = rate_window
        # 07-09-overtrigger P0-3: rate-based open only fires when the rate
        # window has at least this many observations. Default 5 (matches
        # trading_mode.yaml) so a 10-symbol batch is not killed after 3
        # calls. ``llm_config_error`` still opens immediately regardless.
        self._min_rate_samples = max(1, int(min_rate_samples))
        self._state: str = "closed"
        self._consecutive_infra_failures: int = 0
        self._recent_results: list[bool] = []  # True=ok, False=fail
        self._total_attempts: int = 0
        self._successful: int = 0
        self._failed: int = 0
        self._skipped_by_breaker: int = 0
        self._by_category: dict[str, int] = {}
        self._state_transitions: list[dict[str, Any]] = []
        self._total_retries: int = 0
        # Phase C (07-09): repairable schema-alias events that were either
        # repaired successfully or are pending re-validation. These do NOT
        # count toward consecutive_infra_failures or the rate window - a
        # sustained stream of LLM alias emissions must not open the breaker
        # the way a sustained stream of transport/empty/config errors does.
        # The wall-clock budget still bounds total batch time.
        self._repairable_count: int = 0
        # Config name / model cached by the retry wrapper
        self._llm_config_name: str | None = None
        self._llm_model: str | None = None

    # -- public API --

    def should_call(self) -> bool:
        """Return True if an LLM call is allowed."""
        if not self._enabled:
            return True  # disabled breaker never blocks
        return self._state != "open"

    def record_attempt(
        self,
        *,
        category: str | None,
        ok: bool,
        repairable: bool = False,
    ) -> None:
        """Record the outcome of an LLM attempt. May transition state.

        ``repairable=True`` marks the attempt as a schema-alias / unwrap
        repair event (either successfully repaired, or pending re-validation).
        Per design §7.2 such events are NOT physical provider calls: they do
        NOT increment ``total_attempts`` or ``successful`` and do NOT drive
        the breaker state machine (no half_open->closed transition, no
        consecutive-infra reset, no rate-window push). They are tracked only
        in ``repairable_count`` (and ``by_category`` for diagnostics) so the
        report can separate "3 physical calls + 1 repair" from "4 successful
        products" (production-evidence.md Fact 3). The physical call's own
        ``record_attempt(category=None, ok=True)`` on the success path drives
        the breaker state machine - the repair is a post-hoc normalization,
        not a second probe.
        """
        if not self._enabled:
            return

        if repairable:
            # 07-10 Phase E (design §7.2): repairable events do NOT inflate
            # total_attempts / successful and do NOT touch the breaker state
            # machine. The wall-clock budget still bounds total batch time.
            self._repairable_count += 1
            if category:
                self._by_category[category] = self._by_category.get(category, 0) + 1
            return

        # Physical provider call outcome (Attempt 1 / retry). Only these
        # increment total_attempts and drive the breaker state machine.
        self._total_attempts += 1
        if ok:
            self._successful += 1
            self._consecutive_infra_failures = 0
            self._recent_results.append(True)
            if self._state == "half_open":
                self._transition("closed", reason="half_open_probe_success")
        else:
            self._failed += 1
            self._recent_results.append(False)
            if category:
                self._by_category[category] = self._by_category.get(category, 0) + 1

            # Immediate open on config error
            if category == "llm_config_error":
                self._transition("open", reason="llm_config_error_immediate")
                return

            # Count consecutive infra failures (transport + empty +
            # tool-call-no-text). 07-09-overtrigger R5/R6: a sustained
            # stream of ``llm_tool_call_no_text`` (model hallucinating tool
            # calls despite no tools being exposed) is a real model/prompt
            # defect and should still be able to open the breaker on the
            # consecutive-infrastructure path, even before ``min_rate_samples``
            # is reached. The wall-clock budget still bounds total batch
            # time so this cannot deadlock.
            if category in ("llm_transport_error", "llm_empty_response", "llm_tool_call_no_text"):
                self._consecutive_infra_failures += 1
                if self._consecutive_infra_failures >= self._consecutive_threshold:
                    self._transition(
                        "open",
                        reason=f"{self._consecutive_threshold}_consecutive_{category}",
                    )
                    return
            else:
                self._consecutive_infra_failures = 0

            # Rate-based open: >= rate_threshold failures in latest rate_window calls.
            # 07-09-overtrigger P0-3: require at least ``min_rate_samples``
            # observations before the rate check fires. Pre-fix the check
            # used a hardcoded ``len(window) >= 3`` floor, so [fail, fail,
            # success] (3 samples, 67% rate) opened the breaker and skipped
            # the remaining 7-8 symbols of a 10-symbol batch.
            window = self._recent_results[-self._rate_window:]
            if len(window) >= self._min_rate_samples:
                fail_rate = sum(1 for r in window if not r) / len(window)
                if fail_rate >= self._rate_threshold:
                    self._transition("open", reason=f"failure_rate_{fail_rate:.0%}")

    def record_skip(self) -> None:
        """Record a symbol skipped because the breaker was open."""
        self._skipped_by_breaker += 1

    def record_retry(self) -> None:
        """Record one retry recovery call (attempt 2 or 3)."""
        self._total_retries += 1

    @property
    def state(self) -> str:
        return self._state

    @property
    def llm_config_name(self) -> str | None:
        return self._llm_config_name

    @llm_config_name.setter
    def llm_config_name(self, value: str | None) -> None:
        self._llm_config_name = value

    @property
    def llm_model(self) -> str | None:
        return self._llm_model

    @llm_model.setter
    def llm_model(self, value: str | None) -> None:
        self._llm_model = value

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable summary for analysis_batches.summary_json."""
        dominant = ""
        if self._by_category:
            dominant = max(self._by_category, key=self._by_category.get)  # type: ignore[arg-type]
        # Expose the latest-10 failure rate so diagnostics can evaluate the
        # breaker-open condition post-hoc (PRD AC18 / R3). Trims to the
        # rate_window (default 10) and computes the failure rate over that
        # window, not the whole-batch total.
        window = self._recent_results[-self._rate_window:]
        recent_10_failure_rate = (sum(1 for r in window if not r) / len(window)) if window else 0.0
        return {
            "total_attempts": self._total_attempts,
            "successful": self._successful,
            "failed": self._failed,
            "skipped_by_breaker": self._skipped_by_breaker,
            "dominant_error_category": dominant,
            "breaker_state": self._state,
            "breaker_state_transitions": list(self._state_transitions),
            "by_category": dict(self._by_category),
            "total_retries": self._total_retries,
            "recent_10_calls": len(window),
            "recent_10_failed": sum(1 for r in window if not r),
            "recent_10_failure_rate": round(recent_10_failure_rate, 3),
            # 07-09-overtrigger P0-3: expose the configured min-rate-samples
            # floor so diagnostics can distinguish "breaker opened at 67%
            # over 3 calls" (the bug) from "breaker opened at 60% over 5
            # calls" (the post-fix behavior).
            "min_rate_samples": self._min_rate_samples,
            # Phase C (07-09): repairable schema-alias events do not count
            # toward the rate window or consecutive infra failures. Exposed
            # here so diagnostics can distinguish "LLM is emitting aliases"
            # from "LLM is genuinely failing".
            "repairable_count": self._repairable_count,
        }

    # -- internal --

    def _transition(self, new_state: str, *, reason: str) -> None:
        if new_state == self._state:
            return
        from_iso = _now_iso()
        self._state_transitions.append({
            "from": self._state,
            "to": new_state,
            "at": from_iso,
            "reason": reason,
        })
        self._state = new_state


class _NullBreaker:
    """No-op breaker for tests and non-controller callers.

    ``should_call()`` always returns True. ``record_attempt`` does nothing.
    Used when ``context`` is None or ``context["llm_breaker"]`` is missing.
    This preserves backward compatibility for tests that call
    ``run_agent_sop_decision`` directly without setting up a breaker.
    """

    def should_call(self) -> bool:
        return True

    def record_attempt(
        self,
        *,
        category: str | None,
        ok: bool,
        repairable: bool = False,
    ) -> None:
        pass

    def record_skip(self) -> None:
        pass

    def record_retry(self) -> None:
        pass

    @property
    def state(self) -> str:
        return "closed"

    @property
    def llm_config_name(self) -> str | None:
        return None

    @llm_config_name.setter
    def llm_config_name(self, value: str | None) -> None:
        pass

    @property
    def llm_model(self) -> str | None:
        return None

    @llm_model.setter
    def llm_model(self, value: str | None) -> None:
        pass

    def snapshot(self) -> dict[str, Any]:
        # Phase C (07-09): mirror the real breaker's keys so consumers
        # reading repairable_count do not KeyError when a _NullBreaker is
        # in play (tests, non-controller callers).
        return {
            "total_attempts": 0,
            "successful": 0,
            "failed": 0,
            "skipped_by_breaker": 0,
            "dominant_error_category": "",
            "breaker_state": "closed",
            "breaker_state_transitions": [],
            "by_category": {},
            "total_retries": 0,
            "recent_10_calls": 0,
            "recent_10_failed": 0,
            "recent_10_failure_rate": 0.0,
            "repairable_count": 0,
            "min_rate_samples": 5,
        }


class BatchRetryBudget:
    """Track remaining retry recovery calls for a batch.

    Each retry attempt (attempt 2 or 3, NOT attempt 1) decrements the
    counter. When the budget reaches 0, no further retries are allowed.
    """

    def __init__(self, *, max_batch_retry_calls: int = 9):
        self._max = max_batch_retry_calls
        self._remaining = max_batch_retry_calls

    def remaining(self) -> int:
        return self._remaining

    def consume(self) -> bool:
        """Consume one retry slot. Returns True if budget was available."""
        if self._remaining <= 0:
            return False
        self._remaining -= 1
        return True


class BatchWallClockBudget:
    """PRIMARY scheduler safeguard: cap total batch LLM wall-clock.

    The budget is checked BEFORE every LLM call (Attempt 1, 2, and 3).
    If ``remaining_ms() < estimated_call_ms + jitter_ms``, the call is
    skipped and the symbol goes fail-closed with
    ``llm_fallback_reason="wall_clock_budget_exhausted"``.
    """

    def __init__(self, *, budget_seconds: float = 90.0):
        self._budget_ms = int(budget_seconds * 1000)
        self._start_ms = _now_ms()

    def remaining_ms(self) -> int:
        elapsed = _now_ms() - self._start_ms
        return max(0, self._budget_ms - elapsed)

    def snapshot_remaining_ms(self) -> int:
        """Return remaining ms for observability (snapshot)."""
        return self.remaining_ms()


class PerSymbolDeadline:
    """07-10 Phase B: per-symbol end-to-end wall-clock deadline.

    Replaces the fixed ``ESTIMATED_CALL_MS=30_000`` admission gate that
    starved the last 7 of 10 symbols under a shared 90s batch budget. Each
    symbol gets its OWN bounded window covering the full attempt chain
    (Attempt 1 + retries + jitter + parse + validate). The deadline starts
    immediately before Attempt 1 and ends after success or terminal fallback.

    The monotonic clock is INJECTABLE so deterministic tests can drive the
    deadline through a fake clock (no real ``time.sleep(180)``). In
    production the default ``_now_ms`` is used.

    ``provider_timeout_ms(attempt_timeout_seconds)`` derives each provider
    call's timeout from the REMAINING symbol time, capped at the configured
    per-attempt timeout:

        min(per_attempt_timeout_ms, remaining_ms())

    This guarantees every provider call is bounded by the remaining symbol
    deadline and that the symbol never exceeds its configured cap. Early
    success returns immediately (no forced 3-min wait) — the deadline is an
    upper bound, not a minimum.
    """

    def __init__(
        self,
        *,
        per_symbol_timeout_seconds: int = 300,
        per_attempt_timeout_seconds: int = 180,
        now_ms: Any | None = None,
    ) -> None:
        # Reject bool (subclass of int) and float/str — match loader validation.
        if isinstance(per_symbol_timeout_seconds, bool) or not isinstance(per_symbol_timeout_seconds, int):
            raise ValueError(
                "per_symbol_timeout_seconds 必须是整数；"
                f"got {per_symbol_timeout_seconds!r}"
            )
        if isinstance(per_attempt_timeout_seconds, bool) or not isinstance(per_attempt_timeout_seconds, int):
            raise ValueError(
                "per_attempt_timeout_seconds 必须是整数；"
                f"got {per_attempt_timeout_seconds!r}"
            )
        if not (180 <= per_symbol_timeout_seconds <= 1200):
            raise ValueError(
                "per_symbol_timeout_seconds 必须 ∈ [180, 1200]（不允许静默截断）；"
                f"got {per_symbol_timeout_seconds}"
            )
        if per_attempt_timeout_seconds <= 0:
            raise ValueError(
                "per_attempt_timeout_seconds 必须为正整数；"
                f"got {per_attempt_timeout_seconds}"
            )
        if per_attempt_timeout_seconds > per_symbol_timeout_seconds:
            raise ValueError(
                "per_attempt_timeout_seconds 必须 <= per_symbol_timeout_seconds "
                f"({per_attempt_timeout_seconds} > {per_symbol_timeout_seconds})"
            )
        self._per_symbol_ms = per_symbol_timeout_seconds * 1000
        self._per_attempt_ms = per_attempt_timeout_seconds * 1000
        # Inject the clock for deterministic tests; default to module _now_ms.
        self._now_ms = now_ms if now_ms is not None else _now_ms
        self._start_ms = self._now_ms()

    def remaining_ms(self) -> int:
        """Remaining wall-clock budget for this symbol (ms), floored at 0."""
        elapsed = self._now_ms() - self._start_ms
        return max(0, self._per_symbol_ms - elapsed)

    def provider_timeout_ms(self) -> int:
        """Provider call timeout for the NEXT attempt (ms).

        ``min(per_attempt_timeout_ms, remaining_ms())``. Guarantees every
        provider call is bounded by the remaining symbol deadline.
        """
        return min(self._per_attempt_ms, self.remaining_ms())

    def exhausted(self) -> bool:
        """True when the symbol deadline has elapsed (remaining_ms() == 0)."""
        return self.remaining_ms() <= 0

    def elapsed_ms(self) -> int:
        """Wall-clock ms consumed by this symbol so far."""
        return max(0, self._now_ms() - self._start_ms)

    def snapshot(self) -> dict[str, Any]:
        """Observability snapshot for diagnostics / batch summary."""
        return {
            "per_symbol_timeout_ms": self._per_symbol_ms,
            "per_attempt_timeout_ms": self._per_attempt_ms,
            "elapsed_ms": self.elapsed_ms(),
            "remaining_ms": self.remaining_ms(),
            "exhausted": self.exhausted(),
        }


class SingleFlightLease:
    """07-10 Phase B / R4: single-flight lease keyed by ``symbol`` only.

    Prevents two scheduler ticks (even for *different* ``batch_id``s) from
    launching duplicate work for the same symbol at once, and prevents a
    long-running symbol (whose per-symbol timeout can exceed the scheduler
    interval) from being re-launched by the next tick while still in flight.

    The lease is a process-local registry keyed by ``symbol`` alone — NOT
    ``(batch_id, symbol)``. Rationale (design §6.4, R4): the same physical
    market symbol should never be analyzed concurrently by two ticks, because
    the downstream decision persistence + paper-order / signal-alert side
    effects are keyed by symbol and a second concurrent analysis would race
    the first (overwriting the fresher decision, double-firing alerts, etc.).
    A cross-batch overlap is exactly the overlap case that must be blocked,
    not allowed.

    ``acquire`` returns True (and registers) when no lease is held for that
    symbol, False when one is. ``release`` drops the lease. The lease is
    thread-safe: the fair scheduler's coordinator acquires on the coordinator
    thread and releases in a ``finally`` after persistence, but the bounded
    ``ThreadPoolExecutor`` worker threads share the process and the lease may
    also be observed by a concurrent tick's coordinator — so all mutations are
    guarded by an internal lock. (Cross-process overlap is handled by the
    existing ``agent_jobs`` pending/running guard in
    ``enqueue_market_analysis``; this lease adds the in-process per-tick guard
    the fair scheduler needs.)

    For backward compatibility with callers/tests that still pass ``batch_id``,
    the ``batch_id`` keyword argument is accepted but IGNORED — the key is the
    symbol only. New callers should omit ``batch_id``.
    """

    def __init__(self) -> None:
        self._held: set[str] = set()
        self._lock = threading.Lock()

    def acquire(self, *, symbol: str, batch_id: str | None = None) -> bool:
        with self._lock:
            if symbol in self._held:
                return False
            self._held.add(symbol)
            return True

    def release(self, *, symbol: str, batch_id: str | None = None) -> None:
        with self._lock:
            self._held.discard(symbol)

    def is_held(self, *, symbol: str, batch_id: str | None = None) -> bool:
        with self._lock:
            return symbol in self._held

    def held_count(self) -> int:
        with self._lock:
            return len(self._held)

    def clear(self) -> None:
        """Drop every held lease (test reset hook). Production never calls this:
        the global lease is meant to persist across ticks for the cross-batch
        mutex. Tests that want a clean slate (no carry-over from a prior test)
        call this to avoid order-dependent false greens."""
        with self._lock:
            self._held.clear()


# 07-10 S5 (P1 #5): PROCESS-LEVEL single-flight lease singleton. The prior
# design stored the lease per ``_batch_breakers[batch_id]`` in
# ``run_ga_workers``, which gave EVERY batch_id its OWN lease instance -> two
# overlapping ticks for the SAME symbol but DIFFERENT batch_ids each acquired
# cleanly on their own isolated lease -> NO cross-batch mutex (the P1 #5 hole).
# CryptoGuard runs a single process (4 daemon worker threads via
# ``service_manager._spawn``; no multi-process/docker/systemd fan-out), so a
# MODULE-LEVEL singleton is the correct scope: every ``process_fair_batch`` call
# shares this one registry, and a symbol held by batch A's tick is visible as
# held to batch B's tick. ``process_fair_batch`` acquires through this singleton
# (via ``run_fair_batch``) and releases ONLY after per-symbol persistence +
# ``_post_decision_effects`` complete, so the mutex covers the whole
# decision-write + side-effect window, not just the LLM-call window.
_GLOBAL_LEASE: SingleFlightLease = SingleFlightLease()


def global_single_flight_lease() -> SingleFlightLease:
    """Return the process-level ``SingleFlightLease`` singleton (S5, P1 #5).

    ``process_fair_batch`` passes this into ``run_fair_batch`` so the
    cross-batch same-symbol mutex is shared across every tick in the process.
    Direct unit/integration callers that want an ISOLATED lease (e.g. the
    Phase C scheduler tests that construct ``SingleFlightLease()`` themselves)
    keep passing their own instance — ``run_fair_batch`` never reaches for the
    singleton implicitly; the caller chooses. This keeps the singleton's
    cross-tick state out of isolated unit tests (which would otherwise see
    leases leaked from a prior test and flip to order-dependent false greens)."""
    return _GLOBAL_LEASE


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
