# -*- coding: utf-8 -*-
"""08-04 Codex terminal-review blocking-P2 fix (contract D4 market path).

The Codex terminal review REVOKED the prior zero-findings conclusion with a
BLOCKING P2: the three market-decision builders
(``build_llm_decision_prompt`` / ``build_llm_strict_json_prompt`` /
``build_llm_minimal_safe_prompt``) prepend ``SYSTEM_PROMPT`` or
``SYSTEM_PROMPT_STRICT_JSON`` into the user message while the provider also
sets ``session.system`` — violating D4 ("system prompt ONLY in
``session.system``, never repeated in the user prompt"). It cannot be
exempted as historical/out-of-scope.

The fix (this file's contract): the three market builders return the user
message ALONE (the structured JSON input payload), and explicitly select this
round's system prompt by stashing a one-shot ``system_override`` into the
thread-local ``_llm_call_state`` (consumed by ``_call_ga_llm`` before
``_llm_call_state_reset``). The retry tiers select:
  attempt 1        -> SYSTEM_PROMPT          (build_llm_decision_prompt)
  attempt 2 (json) -> SYSTEM_PROMPT_STRICT_JSON (build_llm_strict_json_prompt)
  attempt 3 (safe) -> SYSTEM_PROMPT_STRICT_JSON (build_llm_minimal_safe_prompt)

RED-first + revert-fail: every assertion here FAILS against the pre-fix code
(the builders embed the system prompt in the user text and never stash
``system_override``, so attempt-2/3 in-process AND subprocess
``session.system`` fall back to ``SYSTEM_PROMPT`` and the user message
duplicates the system) and passes after the fix. Reverting any builder
override stash, the subprocess forwarding, or the one-shot cleanup makes the
corresponding test genuinely fail (R10). The cleanup contracts (R8) are
proven by REVERT-FAIL: the pre-fix builders never stash an override, so the
leak is unobservable until the fix installs the stash — reverting the
cleanup line fails the test.

No production DB mutation, no marker write, no service restart, no commit.
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugins.crypto_guard.reasoning.llm_breaker import _NullBreaker

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

_ANALYSIS_TIME_UTC = 1785487499999


def _judge():
    from plugins.crypto_guard.reasoning import llm_agent_judge
    return llm_agent_judge


def _snapshot() -> dict:
    at = _ANALYSIS_TIME_UTC
    health = {tf: {"ready": True, "last_close_time": at - 60_000}
              for tf in ("1d", "4h", "1h", "15m")}
    profiles = {tf: {"market_structure": "bullish", "momentum": "bullish"}
                for tf in ("1d", "4h", "1h", "15m")}
    return {
        "symbol": "SOLUSDT",
        "analysis_time_utc": at,
        "profiles": profiles,
        "modules": {"momentum": {"direction": "bullish"}},
        "data_quality": {"health_by_tf": health},
    }


def _deterministic() -> dict:
    return {
        "decision": "monitor_only",
        "signal_grade": "B",
        "market_bias": "neutral",
        "trend_stage": "range",
        "confidence": 0.5,
        "symbol": "SOLUSDT",
        "analysis_time_utc": _ANALYSIS_TIME_UTC,
    }


def _fake_cfg() -> SimpleNamespace:
    return SimpleNamespace(trading_mode={"risk": {}})


def _gen_cfg() -> dict:
    return {"thinking_budget_tokens": 0, "max_output_tokens": 100,
            "temperature": 0, "max_prompt_bytes": 48 * 1024}


def _valid_decision_json() -> str:
    payload = {
        "symbol": "SOLUSDT",
        "analysis_time_utc": _ANALYSIS_TIME_UTC,
        "decision": "monitor_only",
        "signal_grade": "B",
        "market_bias": "neutral",
        "trend_stage": "range",
        "confidence": 0.5,
        "summary": "观察.",
        "evidence": ["1H 反弹"],
        "counter_evidence": ["1D 仍下行"],
        "has_trade_plan": False,
        "opportunity_watch": None,
        "suggested_actions": ["monitor_only"],
    }
    return json.dumps(payload, ensure_ascii=False)


def _fallback():
    judge = _judge()
    return judge.run_agent_sop_decision(_snapshot(), use_llm=False)


def _build(builder):
    """Call a market builder under the config patch; return its output."""
    judge = _judge()
    with patch("plugins.crypto_guard.config.loader.load_config",
               return_value=_fake_cfg()):
        return builder(_snapshot(), _deterministic(), context=None)


def _captured_call(body: str, want_system: str) -> tuple[str, str]:
    """Drive the REAL ``_call_ga_llm(body)`` with a captured session and
    return ``(session.system, user_text)``."""
    judge = _judge()
    captured = MagicMock()
    captured.raw_ask.return_value = ["{}"]
    # A real provider session always has a numeric ``read_timeout``; a bare
    # MagicMock leaves it as an auto-mock, which the ``< 60`` floor guard in
    # ``_call_ga_llm`` cannot compare. Give the fixture a real value so the
    # guard short-circuits exactly as in production (this test calls
    # ``_call_ga_llm`` directly, without stashing a provider timeout).
    captured.read_timeout = 60
    with patch.object(judge, "_resolve_generation_config", return_value=_gen_cfg()):
        with patch("llmcore.resolve_session", return_value=captured):
            judge._call_ga_llm(body)
    raw_args = captured.raw_ask.call_args
    assert raw_args is not None, "D4: _call_ga_llm must invoke raw_ask"
    content = raw_args[0][0]
    user_text = content[0]["content"][0]["text"]
    return captured.system, user_text


def _symbol(payload: dict) -> object:
    """Extract the symbol from a market-decision payload. The main/strict tiers
    carry it under ``deterministic_reference``/``market_snapshot``; the
    minimal-safe tier surfaces it at the top level."""
    return (
        payload.get("symbol")
        or (payload.get("deterministic_reference") or {}).get("symbol")
        or (payload.get("market_snapshot") or {}).get("symbol")
    )


# ── R1 / R4 / R6: the three tiers return the user payload ALONE ───────────


class TestMarketBuildersReturnUserPayloadOnly:
    """R1 (attempt 1) + R4 (attempt 2/3): the three market builders must
    return the structured JSON input payload ONLY — never embedding
    SYSTEM_PROMPT / SYSTEM_PROMPT_STRICT_JSON / the ``输入：`` separator — and
    must explicitly select this round's system prompt via a one-shot
    ``system_override`` on the thread-local (R6: every tier's user message is
    the pure payload)."""

    def test_attempt1_builder_returns_pure_json_payload(self) -> None:
        judge = _judge()
        body = _build(judge.build_llm_decision_prompt)
        # It must be a valid JSON object (the structured input payload).
        payload = json.loads(body)
        assert isinstance(payload, dict), (
            "D4 R1: attempt-1 user message must be the structured JSON payload; "
            f"got {body[:80]!r}"
        )
        assert _symbol(payload) == "SOLUSDT"
        # No system prompt, no strict prompt, no legacy separator.
        assert judge.SYSTEM_PROMPT not in body, (
            "D4 R1: attempt-1 user message must NOT embed SYSTEM_PROMPT"
        )
        assert judge.SYSTEM_PROMPT_STRICT_JSON not in body, (
            "D4 R1: attempt-1 user message must NOT embed SYSTEM_PROMPT_STRICT_JSON"
        )
        assert "\n\n输入：\n" not in body and "输入：" not in body, (
            "D4 R1: attempt-1 user message must NOT carry the legacy 输入： separator"
        )
        # The builder explicitly selects this round's system prompt.
        assert getattr(judge._llm_call_state, "system_override", None) == (
            judge.SYSTEM_PROMPT
        ), "D4 R1: attempt-1 must select SYSTEM_PROMPT via one-shot system_override"

    def test_attempt23_builders_return_pure_json_payload(self) -> None:
        judge = _judge()
        body2 = _build(judge.build_llm_strict_json_prompt)
        body3 = _build(judge.build_llm_minimal_safe_prompt)
        p2 = json.loads(body2)
        p3 = json.loads(body3)
        assert isinstance(p2, dict) and isinstance(p3, dict), (
            "D4 R4: attempt-2/3 user messages must be the structured JSON payload"
        )
        assert _symbol(p2) == "SOLUSDT" and _symbol(p3) == "SOLUSDT", (
            "D4 R4: retry-tier payloads must still carry the symbol"
        )
        assert judge.SYSTEM_PROMPT_STRICT_JSON not in body2, (
            "D4 R4: attempt-2 user message must NOT embed SYSTEM_PROMPT_STRICT_JSON"
        )
        assert judge.SYSTEM_PROMPT_STRICT_JSON not in body3, (
            "D4 R4: attempt-3 user message must NOT embed SYSTEM_PROMPT_STRICT_JSON"
        )
        assert judge.SYSTEM_PROMPT not in body2 and judge.SYSTEM_PROMPT not in body3, (
            "D4 R4: attempt-2/3 user messages must NOT embed SYSTEM_PROMPT"
        )
        # Strict builder must NOT have performed string-prefix replacement: it
        # shares the SAME payload as the main tier (only the system differs).
        body_main = _build(judge.build_llm_decision_prompt)
        assert json.loads(body_main) == p2, (
            "D4 R4: strict tier must reuse the main tier's payload verbatim "
            "(no string-prefix replacement) — payloads must be identical"
        )
        # Both retry tiers select the STRICT system prompt. Checked right
        # after each builder so the attempt-1 main-tier rebuild does not
        # overwrite the observation.
        body2 = _build(judge.build_llm_strict_json_prompt)
        assert getattr(judge._llm_call_state, "system_override", None) == (
            judge.SYSTEM_PROMPT_STRICT_JSON
        ), "D4 R4: attempt-2 must select SYSTEM_PROMPT_STRICT_JSON"
        body3 = _build(judge.build_llm_minimal_safe_prompt)
        assert getattr(judge._llm_call_state, "system_override", None) == (
            judge.SYSTEM_PROMPT_STRICT_JSON
        ), "D4 R4: attempt-3 must select SYSTEM_PROMPT_STRICT_JSON"


# ── R2 / R5-in-process: real _call_ga_llm puts the tier system in session ─


class TestMarketD4InProcessSessionSystem:
    """R2 (attempt 1) + R5 in-process (attempt 2/3): driving the real
    ``_call_ga_llm`` with the builder output must set ``session.system`` to
    the tier's system prompt AND send a user message that does NOT duplicate
    it (D4 enforced at the provider boundary)."""

    def test_attempt1_inprocess_session_system_is_global(self) -> None:
        judge = _judge()
        body = _build(judge.build_llm_decision_prompt)
        system, user_text = _captured_call(body, judge.SYSTEM_PROMPT)
        assert system == judge.SYSTEM_PROMPT, (
            "D4 R2: attempt-1 in-process session.system must be SYSTEM_PROMPT; "
            f"got {system!r}"
        )
        assert judge.SYSTEM_PROMPT not in user_text, (
            "D4 R2: attempt-1 user message must NOT duplicate the system prompt"
        )
        assert _symbol(json.loads(user_text)) == "SOLUSDT", (
            "D4 R2: attempt-1 user message must be the pure payload"
        )

    def test_attempt23_inprocess_session_system_is_strict(self) -> None:
        judge = _judge()
        for builder in (judge.build_llm_strict_json_prompt,
                        judge.build_llm_minimal_safe_prompt):
            body = _build(builder)
            system, user_text = _captured_call(body, judge.SYSTEM_PROMPT_STRICT_JSON)
            assert system == judge.SYSTEM_PROMPT_STRICT_JSON, (
                "D4 R5: in-process session.system must be SYSTEM_PROMPT_STRICT_JSON "
                f"for retry tier; got {system!r}"
            )
            assert judge.SYSTEM_PROMPT_STRICT_JSON not in user_text, (
                "D4 R5: retry-tier user message must NOT duplicate the strict prompt"
            )
            assert judge.SYSTEM_PROMPT not in user_text, (
                "D4 R5: retry-tier user message must NOT embed the global SYSTEM_PROMPT"
            )


# ── R3 / R5-subprocess: child session.system == tier system, clean user ───


class TestMarketD4SubprocessSessionSystem:
    """R3 (attempt 1) + R5 subprocess (attempt 2/3): the REAL
    ``_run_single_llm_attempt`` (subprocess_hard_timeout=True) must forward
    the tier's system prompt into the child target tuple (the child rebuilds
    ``session.system`` from it) and send a clean user message. The child
    cannot read the parent thread-local, so the forwarding must carry the
    per-tier prompt — the exact P2 defect when it is missing."""

    def _drive(self, builder, want_system) -> tuple[str, str]:
        judge = _judge()
        seen: dict = {}

        def _recorder(target, target_args, *, provider_timeout_seconds, **kwargs):
            seen["target"] = target
            seen["args"] = tuple(target_args)
            return ("ok", _valid_decision_json(), {})

        with patch.object(judge, "_resolve_generation_config", return_value=_gen_cfg()):
            with patch(
                "plugins.crypto_guard.reasoning.llm_agent_judge._run_subprocess_with_target",
                _recorder,
            ):
                with patch("plugins.crypto_guard.config.loader.load_config",
                           return_value=_fake_cfg()):
                    judge._run_single_llm_attempt(
                        snapshot=_snapshot(),
                        fallback=_fallback(),
                        context=None,
                        attempt=1,
                        max_attempts=1,
                        breaker=_NullBreaker(),
                        cfg_name="test_cfg",
                        model_name="test-model",
                        prompt_builders=(builder,),
                        last_category=None,
                        budget_violation_is_skip=True,
                        provider_timeout_seconds=15.0,
                        subprocess_hard_timeout=True,
                        deadline=None,
                    )
        args = seen.get("args")
        assert args is not None, "subprocess test: recorder must capture child tuple"
        assert seen.get("target") is judge._llm_subprocess_target
        return args[0], args[3]  # (user message, system prompt)

    def test_attempt1_subprocess_child_system_is_global_and_user_clean(self) -> None:
        judge = _judge()
        user, system = self._drive(judge.build_llm_decision_prompt, judge.SYSTEM_PROMPT)
        assert system == judge.SYSTEM_PROMPT, (
            "D4 R3: attempt-1 subprocess child session.system must be SYSTEM_PROMPT; "
            f"got {system!r}"
        )
        assert judge.SYSTEM_PROMPT not in user, (
            "D4 R3: attempt-1 subprocess user message must NOT duplicate SYSTEM_PROMPT"
        )
        assert _symbol(json.loads(user)) == "SOLUSDT"

    def test_attempt23_subprocess_child_system_is_strict_and_user_clean(self) -> None:
        judge = _judge()
        for builder in (judge.build_llm_strict_json_prompt,
                        judge.build_llm_minimal_safe_prompt):
            user, system = self._drive(builder, judge.SYSTEM_PROMPT_STRICT_JSON)
            assert system == judge.SYSTEM_PROMPT_STRICT_JSON, (
                "D4 R5-subprocess: retry-tier child session.system must be "
                f"SYSTEM_PROMPT_STRICT_JSON; got {system!r}"
            )
            assert judge.SYSTEM_PROMPT_STRICT_JSON not in user, (
                "D4 R5-subprocess: retry-tier user message must NOT duplicate "
                "the strict prompt"
            )
            assert judge.SYSTEM_PROMPT not in user, (
                "D4 R5-subprocess: retry-tier user message must NOT embed SYSTEM_PROMPT"
            )


# ── R7: prompt_bytes accounts the REAL provider total context ─────────────


class TestPromptBytesTotalContext:
    """R7: prompt_bytes/max_prompt_bytes must still compute the REAL provider
    total context = system bytes + user bytes (NOT under-report to just the
    user body after the D4 split, and NOT silently drop the system)."""

    def test_attempt1_prompt_bytes_is_system_plus_user_total(self) -> None:
        judge = _judge()
        body = _build(judge.build_llm_decision_prompt)
        expected = len(judge.SYSTEM_PROMPT.encode("utf-8")) + len(body.encode("utf-8"))
        actual = getattr(judge._llm_call_state, "prompt_bytes", None)
        assert isinstance(actual, int), "D4 R7: builder must stash prompt_bytes"
        assert actual == expected, (
            "D4 R7: attempt-1 prompt_bytes must be system bytes + user bytes "
            f"(the real provider total); expected {expected}, got {actual}"
        )
        assert actual > len(body.encode("utf-8")), (
            "D4 R7: prompt_bytes must NOT under-report to the user body alone "
            "(system bytes must be included)"
        )

    def test_attempt23_prompt_bytes_is_strict_system_plus_user_total(self) -> None:
        judge = _judge()
        for builder in (judge.build_llm_strict_json_prompt,
                        judge.build_llm_minimal_safe_prompt):
            body = _build(builder)
            expected = (
                len(judge.SYSTEM_PROMPT_STRICT_JSON.encode("utf-8"))
                + len(body.encode("utf-8"))
            )
            actual = getattr(judge._llm_call_state, "prompt_bytes", None)
            assert isinstance(actual, int)
            assert actual == expected, (
                "D4 R7: retry-tier prompt_bytes must be strict-system bytes + "
                f"user bytes; expected {expected}, got {actual}"
            )
            assert actual > len(body.encode("utf-8")), (
                "D4 R7: retry-tier prompt_bytes must include the strict system bytes"
            )


# ── R8: one-shot system_override is cleared on every exit path ────────────


class TestSystemOverrideCleanup:
    """R8: system_override is ONE-SHOT and is cleared on budget-skip,
    deadline-admission-skip, exception, and mocked-``_call_ga_llm`` paths so a
    stale override cannot pollute the next symbol on the same worker thread.

    REVERT-FAIL: the pre-fix builders never stash an override, so these
    assertions pass vacuously against the buggy code (the leak is not yet
    installable). After the fix, reverting the one-shot cleanup (the
    ``_llm_call_input_state_reset`` system_override member or the early-return
    cleanup) makes these tests genuinely fail — the R10 revert proof."""

    def _attempt(self, *, patch_call_ga, deadline=None, max_prompt_bytes=None):
        judge = _judge()
        gen = _gen_cfg()
        if max_prompt_bytes is not None:
            gen = dict(gen, max_prompt_bytes=max_prompt_bytes)
        with patch.object(judge, "_resolve_generation_config", return_value=gen):
            with patch("plugins.crypto_guard.config.loader.load_config",
                       return_value=_fake_cfg()):
                with patch_call_ga:
                    judge._run_single_llm_attempt(
                        snapshot=_snapshot(),
                        fallback=_fallback(),
                        context=None,
                        attempt=1,
                        max_attempts=1,
                        breaker=_NullBreaker(),
                        cfg_name="test_cfg",
                        model_name="test-model",
                        prompt_builders=(judge.build_llm_decision_prompt,),
                        last_category=None,
                        budget_violation_is_skip=True,
                        provider_timeout_seconds=15.0,
                        subprocess_hard_timeout=False,
                        deadline=deadline,
                    )

    def test_budget_skip_clears_system_override(self) -> None:
        judge = _judge()
        self._attempt(
            patch_call_ga=patch.object(judge, "_call_ga_llm", return_value="{}"),
            max_prompt_bytes=10,  # force the budget skip
        )
        assert not hasattr(judge._llm_call_state, "system_override"), (
            "R8: budget-skip path must clear the one-shot system_override"
        )

    def test_deadline_skip_clears_system_override(self) -> None:
        judge = _judge()
        exhausted = SimpleNamespace(exhausted=lambda: True)
        self._attempt(
            patch_call_ga=patch.object(judge, "_call_ga_llm", return_value="{}"),
            deadline=exhausted,
        )
        assert not hasattr(judge._llm_call_state, "system_override"), (
            "R8: deadline-admission-skip path must clear system_override"
        )

    def test_mocked_call_ga_llm_clears_system_override(self) -> None:
        judge = _judge()
        self._attempt(
            patch_call_ga=patch.object(judge, "_call_ga_llm",
                                       return_value=_valid_decision_json()),
        )
        assert not hasattr(judge._llm_call_state, "system_override"), (
            "R8: a mocked _call_ga_llm (that never consumes the override) must "
            "still clear it at the owning attempt boundary"
        )

    def test_exception_clears_system_override(self) -> None:
        judge = _judge()

        def _boom(prompt):
            raise RuntimeError("test boom")

        try:
            self._attempt(patch_call_ga=patch.object(judge, "_call_ga_llm",
                                                     side_effect=_boom))
        except Exception:
            pass
        assert not hasattr(judge._llm_call_state, "system_override"), (
            "R8: an exception in _call_ga_llm must still clear system_override "
            "(finally cleanup)"
        )


# ── R9: concurrent symbols with different tiers do not cross talk ─────────


class TestConcurrentTierIsolation:
    """R9: two concurrent symbols using different attempt tiers must not cross
    system prompts. The thread-local ``_llm_call_state`` gives each thread its
    own one-shot override; driving the REAL ``_call_ga_llm`` on two threads
    must yield session.system = SYSTEM_PROMPT on thread A (attempt 1) and
    SYSTEM_PROMPT_STRICT_JSON on thread B (attempt 3) simultaneously. RED
    pre-fix: thread B's builder embeds the strict prompt in the user text and
    never stashes an override, so its session.system falls back to
    SYSTEM_PROMPT."""

    def test_concurrent_symbols_do_not_cross_system_prompts(self) -> None:
        judge = _judge()
        lock = threading.Lock()
        sessions: dict = {}
        out: dict = {}
        barrier = threading.Barrier(2)

        def _resolve(cfg_name):
            s = MagicMock()
            s.raw_ask.return_value = ["{}"]
            # Real sessions carry a numeric read_timeout (see _captured_call).
            s.read_timeout = 60
            with lock:
                sessions[threading.get_ident()] = s
            return s

        def _worker(name, builder):
            try:
                body = builder(_snapshot(), _deterministic(), context=None)
                barrier.wait(timeout=30)
                judge._call_ga_llm(body)
                with lock:
                    out[name] = sessions[threading.get_ident()].system
            except Exception as exc:  # pragma: no cover - diagnostic only
                with lock:
                    out[name] = exc

        # ALL patches live in ONE process-wide context, never nested per-thread.
        # ``unittest.mock.patch`` mutates a GLOBAL module/instance attribute, so
        # two threads entering/leaving their own patch of the SAME target race:
        # one thread's exit restores the original while the other is still
        # mid-call, and its ``resolve_session`` silently escapes the recorder
        # (no session recorded -> KeyError on read-back). A single outer patch
        # that dispatches per-thread by ``get_ident()`` has no race.
        with patch("llmcore.resolve_session", _resolve), \
             patch.object(judge, "_resolve_generation_config",
                          return_value=_gen_cfg()), \
             patch("plugins.crypto_guard.config.loader.load_config",
                   return_value=_fake_cfg()):
            t_a = threading.Thread(
                target=_worker, name="tierA",
                args=("A", judge.build_llm_decision_prompt),
            )
            t_b = threading.Thread(
                target=_worker, name="tierB",
                args=("B", judge.build_llm_minimal_safe_prompt),
            )
            t_a.start()
            t_b.start()
            t_a.join(timeout=60)
            t_b.join(timeout=60)
        assert not t_a.is_alive() and not t_b.is_alive(), "workers must finish"

        err = out.get("A") if isinstance(out.get("A"), Exception) else None
        assert err is None, f"thread A raised: {err!r}"
        err = out.get("B") if isinstance(out.get("B"), Exception) else None
        assert err is None, f"thread B raised: {err!r}"

        assert out.get("A") == judge.SYSTEM_PROMPT, (
            "R9: thread A (attempt 1) session.system must be SYSTEM_PROMPT; "
            f"got {out.get('A')!r}"
        )
        assert out.get("B") == judge.SYSTEM_PROMPT_STRICT_JSON, (
            "R9: thread B (attempt 3) session.system must be SYSTEM_PROMPT_STRICT_JSON; "
            f"got {out.get('B')!r}"
        )
        assert out.get("A") != out.get("B"), (
            "R9: the two tiers' system prompts must not cross"
        )
