# -*- coding: utf-8 -*-
"""08-04 reviewer round-2 (G) fixes — RED-first.

G2 (P1-1, contract D4 subprocess): when the fair adapter opts into process
isolation (``subprocess_hard_timeout``), ``_call_ga_llm`` must forward the
resolved per-task system prompt into ``_run_provider_call_in_subprocess`` so
the child rebuilds ``session.system`` with the SAME per-task prompt (the child
cannot see the parent's thread-local). Without this, removing the user-message
prepend would silently drop the per-task prompt for subprocess calls. The
in-process D4 contract (user message = JSON body alone, prompt in
``session.system``) is asserted in ``test_pg_08_04_prompt_audit_d.py``.

G3 (P2-1, contract C watch context): ``build_llm_decision_prompt`` must read
the deterministic ``watch_reason`` column (the repository returns ``SELECT *``
rows keyed ``watch_reason``) into ``active_watches[].reason``. Reading the
non-existent ``reason`` key yields None and silently drops the deterministic
reason the user sees when a watch triggers.

RED-first + revert-fail: every assertion here fails against the pre-fix code
(G2: no ``system_prompt`` kwarg on the subprocess call; G3:
``active_watches[].reason`` is None) and passes after the fix. No production DB
mutation, no marker write, no service restart, no commit.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

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


# ── G2 (P1-1, contract D4): subprocess path receives the per-task prompt ─────


class TestD4SubprocessForwardsSystemPrompt:
    """G2: ``_call_ga_llm`` must pass the resolved per-task system prompt into
    the subprocess wrapper so the child ``session.system`` matches the
    in-process path (the child cannot read the parent's thread-local)."""

    def test_subprocess_call_receives_task_prompt(self) -> None:
        judge = _judge()
        captured = MagicMock()
        captured.raw_ask.return_value = ["{}"]
        task_prompt = judge.TASK_SYSTEM_PROMPTS["opportunity_watch_review"]
        received = {}

        def _recorder(prompt, *, provider_timeout_seconds, cfg_name,
                      effective_out, system_prompt=None):
            received["system_prompt"] = system_prompt
            return "{}"

        judge._llm_call_state.system_override = task_prompt
        judge._llm_call_state.provider_timeout_seconds = 15.0
        judge._llm_call_state.subprocess_hard_timeout = True
        try:
            with patch.object(judge, "_resolve_llm_config_name", return_value="test"):
                with patch("llmcore.resolve_session", return_value=captured):
                    with patch.object(
                        judge, "_run_provider_call_in_subprocess", _recorder
                    ):
                        judge._call_ga_llm("user payload text")
        finally:
            for _a in ("system_override", "provider_timeout_seconds",
                       "subprocess_hard_timeout"):
                if hasattr(judge._llm_call_state, _a):
                    delattr(judge._llm_call_state, _a)

        assert received.get("system_prompt") == task_prompt, (
            "D4: _call_ga_llm must forward the per-task system prompt to the "
            f"subprocess wrapper; got {received.get('system_prompt')!r}"
        )
        assert received.get("system_prompt") != judge.SYSTEM_PROMPT, (
            "D4: subprocess must get the per-task prompt, not the global fallback"
        )

    def test_provider_wrapper_forwards_system_prompt_to_child_target(self) -> None:
        """G2 link-2: the REAL ``_run_provider_call_in_subprocess`` must pass
        the per-task system prompt into the child target's args tuple (the
        generic runner ``_run_subprocess_with_target`` is patched to a recorder
        so we pin the tuple WITHOUT spawning a real child). If this line
        regressed to ``SYSTEM_PROMPT``, every subprocess task call would
        silently drop the per-task prompt and the link-1 test would still pass."""
        judge = _judge()
        task_prompt = judge.TASK_SYSTEM_PROMPTS["opportunity_watch_review"]
        seen: dict = {}

        def _recorder(target, target_args, *, provider_timeout_seconds, **kwargs):
            seen["target"] = target
            seen["args"] = tuple(target_args)
            return ("ok", "{}", {})

        with patch(
            "plugins.crypto_guard.reasoning.llm_agent_judge._run_subprocess_with_target",
            _recorder,
        ):
            judge._run_provider_call_in_subprocess(
                "user payload text",
                provider_timeout_seconds=15.0,
                cfg_name="test",
                effective_out={},
                system_prompt=task_prompt,
            )

        assert seen.get("target") is judge._llm_subprocess_target, (
            "link-2: wrapper must run the real production child target"
        )
        args = seen.get("args")
        assert args is not None, "link-2: recorder must capture target_args"
        assert args[3] == task_prompt, (
            "link-2: _run_provider_call_in_subprocess must forward the per-task "
            f"system prompt into the child target tuple; got {args[3]!r}"
        )
        assert args[3] != judge.SYSTEM_PROMPT, (
            "link-2: child target must get the per-task prompt, not the global fallback"
        )

    def test_child_target_sets_session_system_to_task_prompt(self) -> None:
        """G2 link-3: the child ``_llm_subprocess_target`` must rebuild
        ``session.system`` with the SAME per-task prompt (the child cannot read
        the parent's thread-local). We drive the real target directly with
        ``sys.modules["llmcore"]`` faked so ``resolve_session`` returns a
        MagicMock session -- the only reliable way to cover the child side,
        because a real spawned child re-imports llmcore and cannot see an outer
        patch. If this line regressed to ``SYSTEM_PROMPT``, the child would
        silently drop the per-task prompt."""
        judge = _judge()
        task_prompt = judge.TASK_SYSTEM_PROMPTS["opportunity_watch_review"]
        session = MagicMock()
        session.raw_ask.return_value = ["{}"]
        fake_conn = MagicMock()
        fake_llmcore = SimpleNamespace(resolve_session=lambda cfg: session)
        gen = {"thinking_budget_tokens": 0, "max_output_tokens": 100,
               "temperature": 0}

        with patch.dict("sys.modules", {"llmcore": fake_llmcore}):
            with patch.object(judge, "_resolve_generation_config", return_value=gen):
                judge._llm_subprocess_target(
                    "user payload text", "test", 15.0, task_prompt, fake_conn
                )

        assert session.system == task_prompt, (
            "link-3: child _llm_subprocess_target must rebuild session.system "
            f"with the SAME per-task prompt; got {session.system!r}"
        )
        assert session.system != judge.SYSTEM_PROMPT, (
            "link-3: child session.system must NOT be the global SYSTEM_PROMPT"
        )


# ── G3 (P2-1, contract C): deterministic watch_reason flows into the prompt ──


class TestDecisionPromptWatchReason:
    """G3: the deterministic ``watch_reason`` column must flow into
    ``active_watches[].reason`` in the decision prompt."""

    def test_active_watches_carry_watch_reason(self) -> None:
        judge = _judge()
        snap = _snapshot()
        det = _deterministic()
        reason = "价格突破 101.00 阻力后回踩"
        watch = {
            "symbol": "SOLUSDT",
            "direction": "long",
            "watch_reason": reason,  # repository SELECT * row key
        }
        ctx = {"active_opportunity_watches": [watch]}
        fake_cfg = SimpleNamespace(trading_mode={"risk": {}})
        with patch(
            "plugins.crypto_guard.config.loader.load_config",
            return_value=fake_cfg,
        ):
            prompt = judge.build_llm_decision_prompt(snap, det, context=ctx)

        # 08-04 Codex-P2 (D4): the market builder returns the structured JSON
        # payload ALONE (system prompt lives only in session.system), so the
        # user message parses directly — no 输入： separator to split on.
        payload = json.loads(prompt)
        aw = payload.get("active_watches")
        assert aw, "decision prompt must include active_watches"
        assert aw[0]["reason"] == reason, (
            "P2-1: active_watches[0].reason must be the deterministic "
            f"watch_reason; got {aw[0].get('reason')!r}"
        )
