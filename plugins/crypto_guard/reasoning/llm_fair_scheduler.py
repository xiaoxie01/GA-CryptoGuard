"""07-10 Phase C: fair LLM batch scheduler with bounded concurrency.

This module replaces the legacy serial+shared-90s-budget execution model with
a per-batch fair-pool coordinator. The coordinator guarantees:

1. **Deterministic rotation** - the symbol order is rotated by a batch-derived
   offset so no symbol is permanently first/last by alphabetical position
   (design §4.2).
2. **Two-pass barriers** - every eligible symbol's Attempt 1 completes or times
   out before any Attempt 2 starts; every Attempt 2 completes or times out
   before any Attempt 3 starts. Retries cannot preempt first-pass
   opportunities (design §4.1, R2).
3. **Bounded concurrency** - provider calls run in a bounded executor (default
   4, range 1..4). Max observed concurrency never exceeds the configured bound
   (design §4.3, R3).
4. **Per-symbol deadline** - each symbol gets its own ``PerSymbolDeadline``;
   the legacy shared ``BatchWallClockBudget`` is no longer the admission gate
   for first attempts (design §3.1, R1).
5. **Single-flight** - at most one active analysis per ``(batch_id, symbol)``
   across overlapping scheduler ticks (R3, R7).
6. **Serialized persistence** - LLM computation runs concurrently, but
   repository writes are serialized through the coordinator in deterministic
   order (design §4.3, R3).
7. **Immutable envelopes** - worker threads return immutable
   ``SymbolLLMResult`` envelopes; the coordinator owns controller gates and
   persistence.
8. **Thread-safe accounting** - the breaker, retry budget, and batch metrics
   are protected by locks so concurrent provider calls cannot corrupt counters
   (design §4.3, R3).

The coordinator is invoked only when ``llm.scheduling.mode == "fair_pool"``.
When the mode is ``legacy_serial``, the original ``process_job`` serial path
runs byte-for-byte unchanged (rollback point - the legacy path is the
known-starving path, kept ONLY for rollback, never as a default).

No real LLM or Binance calls: the provider call and the monotonic clock are
injectable for deterministic tests.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, Future, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable

from plugins.crypto_guard.reasoning.llm_breaker import (
    CircuitBreaker,
    BatchRetryBudget,
    BatchWallClockBudget,
    PerSymbolDeadline,
    SingleFlightLease,
)

# Lightweight stdlib logger — the reasoning layer does not import the
# config-loader-backed ``logging_utils`` (avoids a heavy import chain and
# matches the surrounding idiom in llm_agent_judge.py / llm_breaker.py).
LOGGER = logging.getLogger("crypto_guard.fair_scheduler")


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def rotation_offset(batch_id: str, symbol_count: int) -> int:
    """Deterministic batch-derived rotation offset.

    ``offset = stable_hash(batch_id) % len(symbols)`` (design §4.2). Uses
    SHA-256 so the offset is reproducible across processes and not dependent
    on Python's randomized string hashing. ``symbol_count <= 0`` yields 0.
    """
    if symbol_count <= 0:
        return 0
    digest = hashlib.sha256(batch_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % symbol_count


def rotated_order(symbols: list[str], batch_id: str) -> list[str]:
    """Return the symbols rotated by ``rotation_offset(batch_id, len)``.

    The sorted symbol list is the stable base; rotation prevents permanent
    alphabetical starvation while remaining deterministic and auditable.
    """
    base = sorted(symbols)
    n = len(base)
    if n <= 1:
        return list(base)
    off = rotation_offset(batch_id, n)
    return base[off:] + base[:off]


# ---------------------------------------------------------------------------
# Batch capacity
# ---------------------------------------------------------------------------


def required_concurrency(
    *, symbol_count: int, per_symbol_timeout_seconds: int,
    scheduler_interval_seconds: int, completion_guard_seconds: int,
) -> int:
    """Minimum concurrency to fit all first-attempts inside one scheduler tick.

    ``required = ceil(symbol_count * per_symbol_timeout / effective_batch_deadline)``
    where ``effective_batch_deadline = scheduler_interval - completion_guard``
    (design §3.2). Returns 0 when there are no symbols or the deadline is
    non-positive (caller must then decide degraded mode).
    """
    if symbol_count <= 0:
        return 0
    effective = scheduler_interval_seconds - completion_guard_seconds
    if effective <= 0:
        return 0
    import math
    return int(math.ceil(symbol_count * per_symbol_timeout_seconds / effective))


# ---------------------------------------------------------------------------
# Thread-safe batch accounting
# ---------------------------------------------------------------------------


class BatchMetrics:
    """Thread-safe per-batch physical-call and skip accounting (design §7.2).

    All counters are protected by a single lock so concurrent provider calls
    cannot double-count or lose increments. ``record_provider_call`` /
    ``record_repair`` / ``record_skip`` are the ONLY mutators; ``snapshot`` is
    a point-in-time read for batch summary persistence.
    """

    def __init__(self, *, expected_symbols: int) -> None:
        self._lock = threading.Lock()
        self._expected_symbols = expected_symbols
        self._attempt1_symbols: set[str] = set()
        self._provider_call_count = 0
        self._symbol_success_count = 0
        self._symbol_failed_count = 0
        self._retry_call_count = 0
        self._repair_event_count = 0
        self._budget_skip_count = 0
        self._breaker_skip_count = 0
        self._policy_skip_count = 0
        self._max_observed_concurrency = 0
        self._in_flight = 0

    def record_attempt1(self, symbol: str) -> None:
        with self._lock:
            self._attempt1_symbols.add(symbol)

    def enter_call(self) -> None:
        with self._lock:
            self._in_flight += 1
            if self._in_flight > self._max_observed_concurrency:
                self._max_observed_concurrency = self._in_flight

    def exit_call(self) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)

    def record_provider_call(self) -> None:
        with self._lock:
            self._provider_call_count += 1

    def record_retry_call(self) -> None:
        with self._lock:
            self._retry_call_count += 1

    def record_repair(self) -> None:
        with self._lock:
            self._repair_event_count += 1

    def record_success(self) -> None:
        with self._lock:
            self._symbol_success_count += 1

    def record_failure(self) -> None:
        with self._lock:
            self._symbol_failed_count += 1

    def record_budget_skip(self) -> None:
        with self._lock:
            self._budget_skip_count += 1

    def record_breaker_skip(self) -> None:
        with self._lock:
            self._breaker_skip_count += 1

    def record_policy_skip(self) -> None:
        with self._lock:
            self._policy_skip_count += 1

    def max_observed_concurrency(self) -> int:
        with self._lock:
            return self._max_observed_concurrency

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "expected_symbols": self._expected_symbols,
                "attempt1_symbols": len(self._attempt1_symbols),
                "attempt1_symbol_list": sorted(self._attempt1_symbols),
                "provider_call_count": self._provider_call_count,
                "symbol_success_count": self._symbol_success_count,
                "symbol_failed_count": self._symbol_failed_count,
                "retry_call_count": self._retry_call_count,
                "repair_event_count": self._repair_event_count,
                "budget_skip_count": self._budget_skip_count,
                "breaker_skip_count": self._breaker_skip_count,
                "policy_skip_count": self._policy_skip_count,
                "max_observed_concurrency": self._max_observed_concurrency,
                "first_attempt_coverage": (
                    round(len(self._attempt1_symbols) / self._expected_symbols, 4)
                    if self._expected_symbols > 0 else 0.0
                ),
            }


# ---------------------------------------------------------------------------
# Immutable result envelope (design §4.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolLLMResult:
    """Immutable per-symbol LLM outcome returned by worker threads.

    The coordinator owns controller gates and persistence; worker threads
    only produce this envelope. ``frozen=True`` prevents in-flight mutation.
    """

    symbol: str
    schedule_position: int
    schedule_round: int
    candidate: dict[str, Any] | None
    attempt_meta: dict[str, Any]
    terminal_reason: str | None = None
    provider_calls: int = 0
    latency_ms: int = 0


# ---------------------------------------------------------------------------
# Thread-safe breaker / budget adapters
# ---------------------------------------------------------------------------


def make_thread_safe_breaker(breaker: CircuitBreaker) -> CircuitBreaker:
    """Wrap a CircuitBreaker's mutating methods with a lock.

    The existing ``CircuitBreaker`` mutates ``_consecutive_infra_failures``,
    ``_recent_results``, ``_total_attempts``, ``_successful``, ``_failed``,
    ``_skipped_by_breaker``, ``_by_category``, ``_state``,
    ``_state_transitions``, ``_total_retries``, ``_repairable_count`` without
    synchronization. Phase C runs up to 4 provider calls concurrently against
    the SAME batch breaker, so every mutating method and state-read must be
    guarded.

    Rather than rewrite ``CircuitBreaker`` (which the legacy serial path also
    uses, and which must stay byte-for-byte for rollback parity), we install a
    lock here and wrap the methods that the fair scheduler exercises. The
    wrapper is idempotent: calling it twice installs the same lock family.
    """
    if getattr(breaker, "_phase_c_lock_installed", False):
        return breaker
    lock = threading.RLock()
    breaker._phase_c_lock = lock  # type: ignore[attr-defined]
    breaker._phase_c_lock_installed = True  # type: ignore[attr-defined]

    _wrap = breaker.should_call
    def _ts_should_call() -> bool:
        with lock:
            return _wrap()
    breaker.should_call = _ts_should_call  # type: ignore[assignment]

    _ra = breaker.record_attempt
    def _ts_record_attempt(*, category: Any, ok: bool, repairable: bool = False) -> None:
        with lock:
            _ra(category=category, ok=ok, repairable=repairable)
    breaker.record_attempt = _ts_record_attempt  # type: ignore[assignment]

    _rs = breaker.record_skip
    def _ts_record_skip() -> None:
        with lock:
            return _rs()
    breaker.record_skip = _ts_record_skip  # type: ignore[assignment]

    _rr = breaker.record_retry
    def _ts_record_retry() -> None:
        with lock:
            return _rr()
    breaker.record_retry = _ts_record_retry  # type: ignore[assignment]

    _snap = breaker.snapshot
    def _ts_snapshot() -> dict[str, Any]:
        with lock:
            return _snap()
    breaker.snapshot = _ts_snapshot  # type: ignore[assignment]

    return breaker


def make_thread_safe_retry_budget(budget: BatchRetryBudget) -> BatchRetryBudget:
    """Guard ``BatchRetryBudget.consume`` against concurrent double-spend.

    Pre-fix, two retry threads could both read ``_remaining=1`` and both
    decrement to 0, overspending the retry quota by 1. The lock makes
    consume atomic. Idempotent.
    """
    if getattr(budget, "_phase_c_lock_installed", False):
        return budget
    lock = threading.Lock()
    budget._phase_c_lock = lock  # type: ignore[attr-defined]
    budget._phase_c_lock_installed = True  # type: ignore[attr-defined]
    _consume = budget.consume
    def _ts_consume() -> bool:
        with lock:
            return _consume()
    budget.consume = _ts_consume  # type: ignore[assignment]
    return budget


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


@dataclass
class FairBatchConfig:
    """Resolved scheduling config for one batch (design §3.1)."""

    mode: str
    max_concurrency: int
    per_symbol_timeout_seconds: int
    per_attempt_timeout_seconds: int
    batch_completion_guard_seconds: int
    rotate_start_symbol: bool
    max_attempts_per_symbol: int


def resolve_fair_batch_config(llm_cfg: dict[str, Any]) -> FairBatchConfig:
    """Build a ``FairBatchConfig`` from the loaded ``llm`` config segment.

    Validation already ran in ``_validate_llm_scheduling`` at startup, so this
    only resolves the values the coordinator needs. ``max_attempts`` comes from
    ``llm.retry.max_attempts_per_symbol`` (default 3).
    """
    sched = (llm_cfg.get("scheduling") or {})
    retry = (llm_cfg.get("retry") or {})
    return FairBatchConfig(
        mode=str(sched.get("mode", "fair_pool")),
        max_concurrency=int(sched.get("max_concurrency", 4)),
        per_symbol_timeout_seconds=int(sched.get("per_symbol_timeout_seconds", 300)),
        per_attempt_timeout_seconds=int(sched.get("per_attempt_timeout_seconds", 180)),
        batch_completion_guard_seconds=int(sched.get("batch_completion_guard_seconds", 60)),
        rotate_start_symbol=bool(sched.get("rotate_start_symbol", True)),
        max_attempts_per_symbol=int(retry.get("max_attempts_per_symbol", 3)),
    )


@dataclass
class _SymbolWork:
    """Mutable per-symbol work item threaded through the scheduling rounds."""

    symbol: str
    position: int
    deadline: PerSymbolDeadline
    snapshot: dict[str, Any]
    result: SymbolLLMResult | None = None
    # Round outcomes: list of (attempt, terminal_reason_or_None) per attempt.
    attempts_done: list[int] = field(default_factory=list)
    needs_retry: bool = False
    last_category: str | None = None
    # 07-10 R2-2: set by the barrier when ``fut.result(timeout=)`` fired and
    # the future was best-effort cancelled. A running worker thread CANNOT be
    # killed (Python limitation), so the thread keeps executing the hung
    # provider call until the socket ``read_timeout`` (R2-1) or the mock's
    # sleep returns. When the thread finishes, ``_run_one_attempt`` checks this
    # flag and ABANDONS its result + skips the success/failure metrics so the
    # barrier's terminal ``symbol_timeout`` result is sticky (not overwritten
    # by the late-arriving success). Without this, a hung-then-late-success
    # call would overwrite the timeout result and record a spurious success.
    barrier_cancelled: bool = False


def _build_symbol_deadline(
    cfg: FairBatchConfig, *, now_ms: Callable[[], int] | None = None,
) -> PerSymbolDeadline:
    """Create a fresh per-symbol deadline (one per symbol, per batch)."""
    return PerSymbolDeadline(
        per_symbol_timeout_seconds=cfg.per_symbol_timeout_seconds,
        per_attempt_timeout_seconds=cfg.per_attempt_timeout_seconds,
        now_ms=now_ms,
    )


def run_fair_batch(
    *,
    batch_id: str,
    symbols: list[str],
    snapshots: dict[str, dict[str, Any]],
    cfg: FairBatchConfig,
    breaker: CircuitBreaker,
    retry_budget: BatchRetryBudget,
    wall_clock_budget: BatchWallClockBudget,
    metrics: BatchMetrics,
    lease: SingleFlightLease,
    llm_call_fn: Callable[..., tuple[dict[str, Any] | None, dict[str, Any]]],
    now_ms: Callable[[], int] | None = None,
    executor: ThreadPoolExecutor | None = None,
    scheduler_interval_seconds: int = 900,
    sleep_fn: Callable[[float], None] | None = None,
    release_lease: bool = True,
) -> dict[str, SymbolLLMResult]:
    """Run one batch through the fair-pool coordinator.

    Parameters
    ----------
    batch_id
        The batch identifier (rotation key + single-flight key).
    symbols
        Eligible symbols for this batch (already filtered by the caller).
    snapshots
        ``{symbol: market_state_snapshot}`` - one per symbol.
    cfg
        Resolved ``FairBatchConfig``.
    breaker, retry_budget, wall_clock_budget
        Shared batch state. Made thread-safe here (idempotent).
    metrics
        ``BatchMetrics`` for the batch; mutated under lock.
    lease
        ``SingleFlightLease`` - acquired per symbol before its work starts.
        Keyed by ``symbol`` only (R4-1): prevents duplicate concurrent
        analysis of the same physical symbol across overlapping ticks —
        even when the two ticks carry different ``batch_id``s. Whether the
        lease is RELEASED here is governed by ``release_lease`` (see below).
    release_lease
        07-10 S5 (P1 #5): when True (default), release every acquired symbol's
        lease in the ``finally`` after the batch's LLM work completes (R4-2) —
        the historical behavior every direct-call unit/integration test relies
        on. When False, the caller (production: ``process_fair_batch``) takes
        ownership of the release and MUST release each acquired symbol AFTER
        its per-symbol persistence + ``_post_decision_effects`` finish, so the
        cross-batch mutex covers the whole decision-write + side-effect window
        (not just the LLM-call window). The caller can discover which symbols
        were acquired from the returned result map's keys (every result key was
        acquired; policy-skipped symbols are absent).
    llm_call_fn
        The per-symbol LLM call entrypoint. Signature::

            llm_call_fn(*, snapshot, deadline, breaker, retry_budget,
                        wall_clock_budget, attempt, max_attempts,
                        schedule_position, schedule_round, metrics, context)

        Returns ``(candidate_or_None, attempt_meta)``. The coordinator does
        NOT call the provider directly - this indirection lets tests inject a
        fake provider (no real LLM) and lets production wire the real
        ``run_agent_sop_decision`` context.
    now_ms
        Injectable monotonic clock (ms) for deterministic deadline tests.
    executor
        Optional pre-built ``ThreadPoolExecutor``. If absent, a bounded one is
        created and shut down here.
    scheduler_interval_seconds
        The scheduler tick interval (default 900 = 15m). Used only for the
        capacity/degraded-mode gate (design §3.2).
    sleep_fn
        Injectable sleeper (replaces ``time.sleep`` for retry jitter in tests).

    Returns
    -------
    dict[str, SymbolLLMResult]
        One immutable envelope per symbol, keyed by symbol.

    The coordinator enforces the two-pass barrier invariant: no Attempt 2
    begins until every eligible symbol's Attempt 1 has a terminal outcome
    (success, non-retryable failure, deadline exhaustion, or breaker open).
    """
    make_thread_safe_breaker(breaker)
    make_thread_safe_retry_budget(retry_budget)

    # Capacity gate (design §3.2): if the configured concurrency cannot fit
    # all first-attempts inside the effective batch deadline, the batch enters
    # capacity-degraded mode BEFORE work starts - it does not pretend full LLM
    # analysis was possible. Degraded mode still attempts every symbol in
    # rotation order up to the wall-clock budget, but records the degraded
    # state in metrics so the report (Phase E) can surface it.
    required = required_concurrency(
        symbol_count=len(symbols),
        per_symbol_timeout_seconds=cfg.per_symbol_timeout_seconds,
        scheduler_interval_seconds=scheduler_interval_seconds,
        completion_guard_seconds=cfg.batch_completion_guard_seconds,
    )
    capacity_degraded = bool(required > cfg.max_concurrency and required > 0)

    order = rotated_order(symbols, batch_id) if cfg.rotate_start_symbol else sorted(symbols)
    work_items: list[_SymbolWork] = []
    # 07-10 R4-2: track every symbol we successfully acquired so the finally
    # below can release them on ALL exit paths (success / empty batch /
    # exception). Without this the lease leaks: the next tick's same-symbol
    # acquire would return False forever (cross-batch mutex keyed by symbol).
    acquired_symbols: list[str] = []
    # 07-10 P1-2 (terminal review): structured terminal envelopes for policy-
    # skipped symbols. Pre-P1-2 the two skip branches below only recorded the
    # metric and ``continue`` -> the skipped symbol was absent from the
    # returned ``results`` map -> ``process_fair_batch`` got
    # ``fair_results.get(sym) is None`` -> ``preset_attempt_meta={}`` -> the
    # controller's deterministic fallback carried an EMPTY §8 envelope (no
    # ``llm_terminal_reason``), so the report / Phase F diagnostics could not
    # classify the skip (silent-drop signature). Each skip now synthesizes a
    # ``SymbolLLMResult`` carrying the precise terminal reason so the caller's
    # preset path persists it. These are merged into ``results`` below.
    policy_skip_results: dict[str, SymbolLLMResult] = {}
    for pos, sym in enumerate(order):
        snap = snapshots.get(sym)
        if snap is None:
            # Missing snapshot is a policy skip - never recorded as a parse
            # failure (R2). P1-2: synthesize a structured terminal envelope so
            # the caller persists ``llm_terminal_reason="missing_snapshot"``.
            metrics.record_policy_skip()
            LOGGER.warning("fair_batch %s: missing snapshot for %s -> policy_skip", batch_id, sym)
            policy_skip_results[sym] = _policy_skip_result(sym, -1, "missing_snapshot")
            continue
        # 07-10 R4-1: single-flight keyed by SYMBOL only (cross-batch mutex).
        # If a previous tick — even for a DIFFERENT batch_id — is still
        # analyzing this symbol, skip it as a policy skip rather than
        # launching duplicate concurrent work. The lease is released in the
        # finally below after the batch's LLM work completes (success or
        # failure); the downstream DB write is guarded by the (batch_id,
        # symbol) row key, so releasing once the expensive provider calls are
        # done does not risk a persistence race.
        if not lease.acquire(symbol=sym):
            metrics.record_policy_skip()
            LOGGER.info("fair_batch %s: %s already in flight -> policy_skip", batch_id, sym)
            policy_skip_results[sym] = _policy_skip_result(sym, pos, "single_flight_skipped")
            continue
        acquired_symbols.append(sym)
        work_items.append(_SymbolWork(
            symbol=sym, position=pos,
            deadline=_build_symbol_deadline(cfg, now_ms=now_ms),
            snapshot=snap,
        ))

    if not work_items:
        # acquired_symbols is empty here (1:1 with work_items), so nothing
        # to release before returning. P1-2: if every symbol was policy-skipped
        # (e.g. all already in flight on a re-tick), return the synthesized
        # skip envelopes so the caller persists a structured terminal reason
        # for each instead of falling into the empty-batch no-op (which would
        # leave every expected symbol with NO decision row -> worker_failed).
        return dict(policy_skip_results)

    max_conc = max(1, min(cfg.max_concurrency, 4))
    owns_executor = executor is None
    if owns_executor:
        executor = ThreadPoolExecutor(max_workers=max_conc, thread_name_prefix="fair-llm")

    def _submit_round(work_list: list[_SymbolWork], round_num: int) -> list[_SymbolWork]:
        """Submit one scheduling round (all eligible symbols' attempt N) to
        the bounded executor and block at a barrier until every one completes
        or times out. Returns the list of work items that need the NEXT round
        (retryable failures)."""
        futures: list[tuple[_SymbolWork, Future]] = []
        for w in work_list:
            # Skip symbols whose deadline is already exhausted - they get a
            # terminal symbol_timeout, not another call.
            if w.deadline.exhausted():
                w.result = _terminal_timeout(w, round_num)
                continue
            # Breaker open? Skip with breaker_skipped (R2, design §7.3).
            if not breaker.should_call():
                metrics.record_breaker_skip()
                w.result = SymbolLLMResult(
                    symbol=w.symbol, schedule_position=w.position,
                    schedule_round=round_num, candidate=None,
                    attempt_meta={
                        "llm_status": "skipped",
                        "llm_fallback_reason": "circuit_breaker_open",
                        "llm_attempt_count": len(w.attempts_done),
                        "llm_terminal_reason": "breaker_skipped",
                    },
                    terminal_reason="breaker_skipped",
                )
                continue
            fut = executor.submit(  # type: ignore[union-attr]
                _run_one_attempt,
                work=w, round_num=round_num, cfg=cfg, breaker=breaker,
                retry_budget=retry_budget, wall_clock_budget=wall_clock_budget,
                metrics=metrics, llm_call_fn=llm_call_fn,
            )
            futures.append((w, fut))

        # --- Barrier: wait for every in-flight attempt to terminate ---
        # 07-10 R2-2: bound the barrier wait by the per-symbol deadline's
        # provider timeout + a small guard. ``requests`` ``read_timeout`` is a
        # PER-PACKET timeout (reset between packets), so a slow-but-steady
        # stream can outlast it indefinitely. The R2-1 ``session.max_retries=0``
        # kills the llmcore internal retry loop, but a single hung provider
        # call can still block the worker thread forever. ``fut.result(timeout=)``
        # is the only true wall-clock bound on the total call. On timeout,
        # best-effort ``fut.cancel()`` (cannot interrupt a running thread, but
        # the socket ``read_timeout`` from R2-1 will eventually unblock it) and
        # record a terminal ``symbol_timeout`` so the symbol is not retried and
        # the barrier does not hang.
        for w, fut in futures:
            # 07-10 R2-2: bound the barrier wait by the configured per-attempt
            # timeout + a small guard for result assembly. ``requests``
            # ``read_timeout`` is a PER-PACKET timeout (reset between packets),
            # so a slow-but-steady stream can outlast it indefinitely; the R2-1
            # ``session.max_retries=0`` kills the llmcore internal retry loop,
            # but a single hung provider call can still block the worker thread
            # forever. ``fut.result(timeout=)`` is the only true wall-clock
            # bound on the total call. On timeout, best-effort ``fut.cancel()``
            # (cannot interrupt a running thread, but the socket ``read_timeout``
            # from R2-1 will eventually unblock it) and record a terminal
            # ``symbol_timeout`` so the symbol is not retried and the barrier
            # does not hang.
            #
            # The timeout is derived from ``cfg.per_attempt_timeout_seconds``
            # (the configured per-call cap) rather than the dynamic
            # ``deadline.provider_timeout_ms()`` (which shrinks as the symbol
            # budget drains and would collapse to the floor near deadline
            # exhaustion). Bounded by the static per-attempt cap + guard; floored
            # at a small minimum so a misconfigured near-zero cap cannot
            # busy-loop with ``timeout=0`` (cancelling a fast success). In
            # production the per-attempt cap is 180s so the floor never bites.
            _barrier_timeout_s = max(2.0, float(cfg.per_attempt_timeout_seconds)) + 0.5
            try:
                fut.result(timeout=_barrier_timeout_s)
            except FuturesTimeoutError:
                LOGGER.warning(
                    "fair_batch %s: barrier timeout (%.1fs) for %s; "
                    "cancelling + recording symbol_timeout",
                    batch_id, _barrier_timeout_s, w.symbol,
                )
                fut.cancel()  # best-effort; a running thread can't be killed
                # Mark the work as barrier-cancelled so the still-running worker
                # thread (which cannot be killed) abandons its late result and
                # skips the success/failure metrics when it returns — the
                # timeout result set here is sticky.
                w.barrier_cancelled = True
                if w.result is None:
                    w.result = _terminal_timeout(w, round_num)
                    metrics.record_budget_skip()
            except Exception:
                LOGGER.exception("fair_batch %s: attempt worker crashed for %s", batch_id, w.symbol)
                if w.result is None:
                    w.result = SymbolLLMResult(
                        symbol=w.symbol, schedule_position=w.position,
                        schedule_round=round_num, candidate=None,
                        attempt_meta={
                            "llm_status": "failed",
                            "llm_fallback_reason": "worker_crash",
                            "llm_attempt_count": len(w.attempts_done),
                            "llm_terminal_reason": "worker_crash",
                        },
                        terminal_reason="worker_crash",
                    )
                    metrics.record_failure()

        # Collect retry candidates (only retryable failures with budget +
        # remaining deadline). Non-retryable / success / timeout stay terminal.
        retry_next: list[_SymbolWork] = []
        for w in work_list:
            if w.result is not None and w.needs_retry and not w.deadline.exhausted():
                retry_next.append(w)
        return retry_next

    try:
        # Round 1: Attempt 1 for every symbol.
        round1 = [w for w in work_items]
        retry_after_1 = _submit_round(round1, 1)
        # Round 2: Attempt 2 (strict JSON) for retryable failures.
        retry_after_2: list[_SymbolWork] = []
        if retry_after_1 and cfg.max_attempts_per_symbol >= 2:
            # Mark attempt 2 eligibility; reset needs_retry for the round.
            for w in retry_after_1:
                w.needs_retry = False
            retry_after_2 = _submit_round(retry_after_1, 2)
        # Round 3: Attempt 3 (minimal safe) for remaining retryable failures.
        if retry_after_2 and cfg.max_attempts_per_symbol >= 3:
            for w in retry_after_2:
                w.needs_retry = False
            _submit_round(retry_after_2, 3)
    finally:
        # 07-10 R4-2 / S5 (P1 #5): release every acquired symbol's lease on ALL
        # exit paths (success, retryable exhaustion, worker exception, barrier
        # timeout) -- but ONLY when ``release_lease`` is True. When the caller
        # (production ``process_fair_batch``) passes ``release_lease=False``, it
        # owns the release and performs it AFTER per-symbol persistence +
        # ``_post_decision_effects`` so the cross-batch mutex covers the whole
        # decision-write + side-effect window, not just the LLM-call window
        # (the P1 #5 fix). Direct unit/integration callers use the default True
        # and keep the historical auto-release behavior.
        if release_lease:
            for sym in acquired_symbols:
                try:
                    lease.release(symbol=sym)
                except Exception:
                    LOGGER.exception("fair_batch %s: failed to release lease for %s", batch_id, sym)
        if owns_executor and executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)

    # Build the final result map. Symbols that never got a terminal result
    # (should not happen, but fail-closed) get a retry_exhausted envelope.
    results: dict[str, SymbolLLMResult] = {}
    for w in work_items:
        if w.result is None:
            w.result = SymbolLLMResult(
                symbol=w.symbol, schedule_position=w.position,
                schedule_round=cfg.max_attempts_per_symbol, candidate=None,
                attempt_meta={
                    "llm_status": "failed",
                    "llm_fallback_reason": "retry_exhausted",
                    "llm_attempt_count": len(w.attempts_done),
                    "llm_terminal_reason": "retry_exhausted",
                },
                terminal_reason="retry_exhausted",
            )
            metrics.record_failure()
        results[w.symbol] = w.result

    # 07-10 P1-2 (terminal review): merge the structured policy-skip envelopes
    # into the returned map so every expected symbol has a result entry. The
    # caller (``process_fair_batch``) iterates ``symbols`` and reads
    # ``fair_results.get(sym)``; without these entries a policy-skipped symbol
    # got ``None`` -> empty §8 envelope -> unclassifiable in the report. Note:
    # policy-skipped symbols are NOT in ``acquired_symbols`` (they never
    # acquired a lease), so the caller's release logic correctly skips them
    # (they have nothing to release). The caller also correctly distinguishes
    # them from ``continuity_unavailable`` synthesized symbols (which ARE
    # acquired-tracked-excluded via the ``continuity_unavailable`` set).
    results.update(policy_skip_results)

    # Stash degraded flag + schedule order on the metrics snapshot via a
    # side channel the caller (Phase E report) reads. We attach to the breaker
    # batch_state by returning it separately is not possible here; instead the
    # caller reads metrics.snapshot() and this helper.
    metrics.capacity_degraded = capacity_degraded  # type: ignore[attr-defined]
    metrics.schedule_order = list(order)  # type: ignore[attr-defined]
    metrics.batch_id = batch_id  # type: ignore[attr-defined]
    return results


def _terminal_timeout(w: _SymbolWork, round_num: int) -> SymbolLLMResult:
    """Build a terminal symbol_timeout envelope for an already-exhausted
    deadline (design §7.3, R1)."""
    return SymbolLLMResult(
        symbol=w.symbol, schedule_position=w.position,
        schedule_round=round_num, candidate=None,
        attempt_meta={
            "llm_status": "failed",
            "llm_fallback_reason": "wall_clock_budget_exhausted",
            "llm_attempt_count": len(w.attempts_done),
            "llm_terminal_reason": "symbol_timeout",
        },
        terminal_reason="symbol_timeout",
    )


def _policy_skip_result(symbol: str, position: int, reason: str) -> SymbolLLMResult:
    """07-10 P1-2 (terminal review): build a structured terminal
    ``SymbolLLMResult`` for a POLICY-SKIPPED symbol (missing snapshot OR
    single-flight already-in-flight).

    Pre-P1-2 these branches only called ``metrics.record_policy_skip()`` and
    ``continue`` -> the symbol was ABSENT from the returned ``results`` map ->
    ``process_fair_batch`` saw ``fair_results.get(sym) is None`` -> it passed
    ``preset_candidate=None, preset_attempt_meta={}`` to the controller. The
    controller's preset guard (``{} is not None``) is satisfied, so
    ``run_agent_sop_decision`` took the ``candidate is None`` fail-closed
    deterministic fallback - but with an EMPTY ``attempt_meta`` (``fallback.
    update({})``) that set NO ``llm_terminal_reason``. The persisted §8 envelope
    was therefore indistinguishable from a generic deterministic fallback: the
    report / Phase F diagnostics could NOT classify the symbol as
    ``single_flight_skipped`` / ``missing_snapshot`` (it silently merged into the
    unexplained-residual bucket - the exact silent-drop signature the coverage
    diagnostic exists to catch).

    This helper emits a structured terminal envelope so the caller's
    ``preset_attempt_meta`` carries the PRECISE reason into the persisted §8
    envelope. ``candidate=None`` deliberately: the controller's own
    ``run_agent_sop_decision`` deterministic ``fallback`` (built from the
    request snapshot) is the correct observation for a policy skip - a
    single-flight skip is a legitimate cross-batch dedup (NOT a §11 continuity
    gap requiring ``plan_execution_state="no_candidate"``), so the controller's
    normal deterministic-fallback path handles it. ``llm_status="skipped"``
    (distinct from ``"failed"`` and ``"disabled"``): the symbol is neither a
    failure nor an LLM-disabled decision - it was upstream-deprioritized before
    any work, and the aggregator's ``_LLM_POLICY_SKIP_TERMINAL_REASONS`` set
    classifies it into the ``policy_skip`` bucket (NOT the unexplained residual).

    Parameters
    ----------
    symbol
        The skipped symbol.
    position
        Rotation position (for audit; -1 for missing-snapshot since no work
        item was ever created).
    reason
        ``"missing_snapshot"`` or ``"single_flight_skipped"``.
    """
    return SymbolLLMResult(
        symbol=symbol, schedule_position=position,
        schedule_round=0, candidate=None,
        attempt_meta={
            "llm_status": "skipped",
            "llm_fallback_reason": reason,
            "llm_attempt_count": 0,
            "llm_provider_call_count": 0,
            "llm_latency_ms": 0,
            "llm_prompt_bytes": None,
            "llm_continuity_included": None,
            "llm_model": None,
            "llm_config_name": None,
            "llm_terminal_reason": reason,
            "llm_error_category": None,
            "llm_error_stage": None,
            "llm_error": None,
            "llm_retry_round": None,
            "llm_schedule_round": None,
            "llm_schedule_position": position,
            "llm_effective_thinking_budget_tokens": None,
            "llm_effective_max_output_tokens": None,
            "llm_effective_temperature": None,
            "llm_provider_timeout_ms": None,
        },
        terminal_reason=reason,
    )


def _run_one_attempt(
    *, work: _SymbolWork, round_num: int, cfg: FairBatchConfig,
    breaker: CircuitBreaker, retry_budget: BatchRetryBudget,
    wall_clock_budget: BatchWallClockBudget, metrics: BatchMetrics,
    llm_call_fn: Callable[..., tuple[dict[str, Any] | None, dict[str, Any]]],
) -> None:
    """Worker-thread body for a single (symbol, round) attempt.

    ``llm_call_fn`` does ONE provider call + parse + schema/semantic
    validation and returns ``(candidate_or_None, attempt_meta)``. The
    coordinator (this function) owns the breaker, retry budget, and batch
    metrics — it records exactly one physical provider call (when the call
    actually happened), one success/failure/skip, and one repair event when
    ``attempt_meta`` reports a repair. ``llm_call_fn`` receives the
    ``PerSymbolDeadline`` so the provider timeout is derived from remaining
    symbol time; it returns ``llm_status="skipped"`` with
    ``llm_fallback_reason="wall_clock_budget_exhausted"`` when it declined
    to call the provider (deadline exhausted before the call).
    """
    work.attempts_done.append(round_num)
    if round_num == 1:
        metrics.record_attempt1(work.symbol)
    else:
        # Attempt 2/3 consumes a retry slot. If the batch retry quota is
        # exhausted, terminal retry_budget_exhausted — NO provider call made,
        # NO breaker record, NO retry_call increment (the slot wasn't granted).
        if not retry_budget.consume():
            work.result = SymbolLLMResult(
                symbol=work.symbol, schedule_position=work.position,
                schedule_round=round_num, candidate=None,
                attempt_meta={
                    "llm_status": "failed",
                    "llm_fallback_reason": "retry_budget_exhausted",
                    "llm_attempt_count": len(work.attempts_done) - 1,
                    "llm_terminal_reason": "retry_budget_exhausted",
                },
                terminal_reason="retry_budget_exhausted",
            )
            metrics.record_failure()
            return
        metrics.record_retry_call()
        breaker.record_retry()

    if work.deadline.exhausted():
        work.result = _terminal_timeout(work, round_num)
        metrics.record_budget_skip()
        return

    metrics.enter_call()
    try:
        candidate, attempt_meta = llm_call_fn(
            snapshot=work.snapshot, deadline=work.deadline, breaker=breaker,
            retry_budget=retry_budget, wall_clock_budget=wall_clock_budget,
            attempt=round_num, max_attempts=cfg.max_attempts_per_symbol,
            schedule_position=work.position, schedule_round=round_num,
            context=None,
        )
    finally:
        metrics.exit_call()

    # 07-10 R2-2: if the barrier already timed out this attempt and set
    # ``work.result`` to a terminal ``symbol_timeout`` (with
    # ``barrier_cancelled=True``), ABANDON this late-arriving result. A running
    # thread cannot be killed, so ``llm_call_fn`` may return AFTER the barrier
    # cancellation — its success/failure must NOT overwrite the sticky timeout
    # result, and it must NOT record a provider_call / success / failure (the
    # barrier already recorded a budget_skip). The provider call DID happen
    # physically (the socket eventually returned), but semantically the symbol
    # was already terminated by the deadline; counting it as a success would
    # mask the timeout. Count ONLY the provider_call (it happened) so the
    # ``provider_call_count`` metric stays honest, then return without touching
    # the result.
    if work.barrier_cancelled:
        # The call physically reached the provider (it returned, not skipped).
        if str(attempt_meta.get("llm_status") or "failed") != "skipped":
            metrics.record_provider_call()
        return

    status = str(attempt_meta.get("llm_status") or "failed")
    category = attempt_meta.get("llm_error_category")
    fallback_reason = str(attempt_meta.get("llm_fallback_reason") or "")
    work.last_category = category

    # A provider call happened iff the attempt did NOT short-circuit on a
    # budget/breaker skip. Skipped attempts record a skip, not a call.
    skipped = status == "skipped"
    if not skipped:
        metrics.record_provider_call()

    # Repair events (schema alias repaired) are counted separately and do NOT
    # count as a provider call increment on top of the one above — the repair
    # is part of the same attempt's validation, not a second physical call.
    if attempt_meta.get("llm_repair_event"):
        metrics.record_repair()

    # Feed the breaker the right record(s) per attempted call. Skipped
    # attempts (budget/breaker) record nothing here — the breaker-open
    # skip path above already called record_breaker_skip via should_call.
    # 07-10 R1-1 (P0-1 fix): a repaired success (status==ok AND
    # ``llm_repair_event``) is ONE physical provider call that succeeded
    # after a schema-alias / unwrap repair. Mirroring the legacy
    # ``run_agent_sop_decision`` path (llm_agent_judge.py:223-226), the
    # PHYSICAL success drives the breaker state machine first
    # (``record_attempt(category=None, ok=True)`` increments total_attempts
    # / successful and runs half_open->closed), THEN the repair is recorded
    # as a separate post-hoc normalization
    # (``record_attempt(repairable=True)`` tracks repairable_count only —
    # see CircuitBreaker.record_attempt llm_breaker.py:111-118). Pre-R1-1 the
    # fair path emitted a single ``record_attempt(repairable=True)`` which
    # returned at line 118 WITHOUT incrementing total_attempts/successful —
    # the physical success was silently dropped, diverging from the legacy
    # path (production-evidence.md Fact 3: "3 physical + 1 repair" not
    # "4 successes").
    if not skipped:
        if status == "ok" and candidate is not None and \
                attempt_meta.get("llm_repair_event"):
            breaker.record_attempt(category=None, ok=True)
            breaker.record_attempt(
                category="llm_schema_repairable", ok=True, repairable=True,
            )
        else:
            breaker.record_attempt(
                category=category,
                ok=(status == "ok" and candidate is not None),
                repairable=bool(attempt_meta.get("llm_repair_event")),
            )

    if status == "ok" and candidate is not None:
        metrics.record_success()
        work.result = SymbolLLMResult(
            symbol=work.symbol, schedule_position=work.position,
            schedule_round=round_num, candidate=candidate,
            attempt_meta=dict(attempt_meta),
            terminal_reason=None,
            provider_calls=1,
            latency_ms=int(attempt_meta.get("llm_latency_ms") or 0),
        )
        return

    # Budget skip (provider declined because deadline exhausted). Terminal.
    if fallback_reason == "wall_clock_budget_exhausted":
        metrics.record_budget_skip()
        am = dict(attempt_meta)
        am["llm_terminal_reason"] = "symbol_timeout"
        work.result = SymbolLLMResult(
            symbol=work.symbol, schedule_position=work.position,
            schedule_round=round_num, candidate=None,
            attempt_meta=am,
            terminal_reason="symbol_timeout",
            provider_calls=0,
            latency_ms=int(attempt_meta.get("llm_latency_ms") or 0),
        )
        return

    # 07-10 R1-1 (P0-2): prompt-budget contract violation. The adapter
    # returns ``llm_status="skipped"`` + ``llm_fallback_reason=
    # "prompt_budget_contract_violation"`` because NO provider call was made
    # (the mandatory prompt core exceeded ``max_prompt_bytes`` before the
    # call). It is terminal-non-retryable: the same builder will not shrink
    # the mandatory core, so retrying would loop. Record as a budget skip
    # (no physical call, no breaker event - ``skipped`` short-circuited the
    # breaker.record_attempt block above) and carry the exact structured
    # terminal reason so Phase E accounting counts it as a budget skip, not
    # a parse failure (production-evidence.md Fact 2).
    if fallback_reason == "prompt_budget_contract_violation":
        metrics.record_budget_skip()
        am = dict(attempt_meta)
        am["llm_status"] = "skipped"
        am["llm_terminal_reason"] = "prompt_budget_contract_violation"
        work.result = SymbolLLMResult(
            symbol=work.symbol, schedule_position=work.position,
            schedule_round=round_num, candidate=None,
            attempt_meta=am,
            terminal_reason="prompt_budget_contract_violation",
            provider_calls=0,
            latency_ms=int(attempt_meta.get("llm_latency_ms") or 0),
        )
        return

    # Non-retryable failure categories -> terminal (design §9, R7).
    # 07-10 R1-1 (P0-2 fix): ``llm_prompt_budget_violation`` is
    # non-retryable — the same (or stricter) builder will not shrink the
    # mandatory core, so retrying would loop forever, inflate
    # provider_call_count (line 704) for a call that never reached the
    # provider, and emit a spurious breaker event (line 716). The adapter
    # returns ``llm_status="skipped"`` for budget-contract-violation so
    # the call is recorded as a skip, not a failure; but defend against a
    # mislabeled ``failed`` here by treating the category as terminal.
    _NON_RETRYABLE = {
        "llm_config_error", "llm_schema_validation_failed",
        "llm_semantic_validation_failed", "llm_prompt_budget_violation",
        # 07-10 S4 (P0 #3): a process-isolation hard-killed provider call is
        # terminal ``symbol_timeout`` (via ``_terminal_reason_for``) - stop at
        # attempt 1; retrying would burn attempts on a known-wedged provider.
        "llm_subprocess_hard_timeout",
        # 07-10 R4-P0-2: subprocess FATAL errors are terminal non-retryable.
        # ``_run_single_llm_attempt`` classifies each RuntimeError signature
        # (llm_agent_judge.py:1172-1185) BEFORE ``_classify_llm_failure`` so it
        # is NOT routed to ``llm_transport_error`` (retryable). Without these
        # entries here the coordinator would still retry, spawning a NEW child
        # while the fatal condition persists - amplifying orphans
        # (cleanup_failed), re-violating the IPC byte contract
        # (response_oversized), or re-failing spawn (start_failed). MUST stop
        # the symbol at attempt_count=1 (R4-P0-2).
        "llm_subprocess_cleanup_failed",
        "llm_subprocess_response_oversized",
        "llm_subprocess_start_failed",
    }
    if category in _NON_RETRYABLE:
        metrics.record_failure()
        work.result = SymbolLLMResult(
            symbol=work.symbol, schedule_position=work.position,
            schedule_round=round_num, candidate=None,
            attempt_meta=dict(attempt_meta),
            terminal_reason=_terminal_reason_for(category, fallback_reason),
            provider_calls=1,
            latency_ms=int(attempt_meta.get("llm_latency_ms") or 0),
        )
        return

    # Retryable failure: mark for next round if attempts remain and deadline
    # allows. Otherwise terminal retry_exhausted / symbol_timeout.
    can_retry = (
        round_num < cfg.max_attempts_per_symbol
        and not work.deadline.exhausted()
    )
    if can_retry:
        work.needs_retry = True
        # Provisional envelope; the next round replaces it. Kept so the
        # barrier sees a result if the next round never runs.
        work.result = SymbolLLMResult(
            symbol=work.symbol, schedule_position=work.position,
            schedule_round=round_num, candidate=None,
            attempt_meta=dict(attempt_meta),
            terminal_reason=_terminal_reason_for(category, fallback_reason),
            provider_calls=1,
            latency_ms=int(attempt_meta.get("llm_latency_ms") or 0),
        )
    else:
        metrics.record_failure()
        am = dict(attempt_meta)
        if work.deadline.exhausted():
            tr = "symbol_timeout"
            am["llm_terminal_reason"] = "symbol_timeout"
        else:
            tr = "retry_exhausted"
            am["llm_terminal_reason"] = "retry_exhausted"
        work.result = SymbolLLMResult(
            symbol=work.symbol, schedule_position=work.position,
            schedule_round=round_num, candidate=None,
            attempt_meta=am,
            terminal_reason=tr,
            provider_calls=1,
            latency_ms=int(attempt_meta.get("llm_latency_ms") or 0),
        )


def _terminal_reason_for(category: str | None, fallback_reason: str) -> str:
    """Map a failure category/fallback to the exact structured terminal reason
    (design §9, R7). Never returns the generic ``llm_parse_failed``."""
    if category == "llm_json_parse_failed":
        return "llm_json_parse_failed"
    if category == "llm_schema_validation_failed":
        return "llm_schema_validation_failed"
    # 07-10 S4 (P0 #3): a process-isolation hard-killed provider call is a
    # terminal ``symbol_timeout`` - the provider was wedged for the full
    # provider-timeout window and was hard-killed, so the symbol missed its
    # deadline. NOT retry_exhausted (no retries on a known-bad provider).
    if category == "llm_subprocess_hard_timeout":
        return "symbol_timeout"
    # 07-10 R4-P0-2: subprocess FATAL errors carry their own structured
    # terminal reason (the category itself). They are non-retryable and were
    # classified by signature in ``_run_single_llm_attempt`` - the exact code
    # must propagate to the persisted §8 envelope / Phase F diagnostics, NOT
    # be folded into the generic ``llm_transport_error``.
    if category in (
        "llm_subprocess_cleanup_failed",
        "llm_subprocess_response_oversized",
        "llm_subprocess_start_failed",
    ):
        return category
    if fallback_reason == "wall_clock_budget_exhausted":
        return "symbol_timeout"
    if fallback_reason == "retry_budget_exhausted":
        return "retry_budget_exhausted"
    if fallback_reason == "circuit_breaker_open":
        return "breaker_skipped"
    if category in ("llm_transport_error", "llm_rate_limited",
                    "llm_empty_response", "llm_tool_call_no_text"):
        return category or "llm_transport_error"
    return fallback_reason or "llm_transport_error"
