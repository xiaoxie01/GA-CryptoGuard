"""终审返工 R3 (2026-07-26): timeframe 真 token 边界 (P1-1) + symbol+timeframe
组合解析 (P2-1) + unsupported guard 仅约束消费周期的 intent (P1-2).

RED-first tests for the Codex re-review findings:

P1-1 - the R2 numeric-only boundary ``(?<![0-9])...(?![0-9])`` wrongly matches
timeframe substrings INSIDE English words: ``web3market``->3m, ``v1high``->1h,
``x15months``->15m, ``alpha5model``->5m, ``version4high``->4h. The contract:
  - a timeframe must be a real token bounded by ASCII alphanumerics
    ``(?<![0-9A-Za-z])...(?![0-9A-Za-z])`` - NONE of the five words above may
    surface a supported OR unsupported timeframe.
  - CJK-adjacent timeframes stay recognized (``看一下15m的BTCUSDT`` -> 15m):
    CJK chars are not ASCII alphanumerics, so the boundary passes.
  - ``15m`` must not additionally match ``5m``; case-insensitive; written
    order preserved; deduped.
  - the no-separator ``BTCUSDT15m`` writing is supported ONLY through the
    explicit symbol+timeframe combined parse (P2-1 option A) which also
    yields the symbol - NOT by relaxing the letter boundary globally.

P2-1 - ``parse_intent("分析BTCUSDT15m机会")`` previously returned symbol=None
(the ``\\b`` in SYMBOL_RE fails between ``T`` and ``1`` and between CJK and
ASCII because CJK is ``\\w``). The contract: the combined parse recognizes
BOTH ``symbol=="BTCUSDT"`` AND ``timeframes==["15m"]``, and the REAL handler
(``crypto_handle_text_command``) dispatches ``crypto_analyze_symbol_once``
exactly once with that symbol and那些 display timeframes.

R3 P1-1 entity resolution (second blocker batch, 2026-07-26): three writings
must all resolve to ``symbol="BTCUSDT", timeframes=["15m"]``:
  - ``/analyzeBTCUSDT15m``  (leading slash command glued to symbol+TF).
    RED root cause: the ``\\b`` in ``SYMBOL_RE`` treats ``/analyze`` as a word
    boundary, so ``ANALYZEBTCUSDT`` is one ``\\b``-bounded token; the
    ``ANALYZE`` skip never fires (the skip set is checked against the FULL
    token, not a prefix). Fix: strip a leading slash command BEFORE the entity
    scan so the command word never enters the symbol candidate stream.
  - ``看一下15m的BTCUSDT`` (CJK chars adjacent to a STANDALONE symbol).
    RED root cause: ``\\b`` cannot anchor between CJK and ``B`` (CJK is ``\\w``
    under Python's Unicode word-char semantics), so ``BTCUSDT`` standalone is
    invisible to ``SYMBOL_RE``. Fix: replace ``SYMBOL_RE``'s ``\\b`` with an
    ASCII-alphanumeric boundary ``(?<![0-9A-Za-z])([A-Za-z]{2,12}(?:USDT)?)
    (?![0-9A-Za-z])`` (the SAME discipline as the timeframe regexes) so CJK
    adjacency does not break the anchor.
  - ``分析BTCUSDT15m机会`` (the existing P2-1 case) must KEEP working unchanged.
The combined ``_COMBINED_SYMBOL_TF_RE`` still requires the strict ``USDT``
quote suffix; the timeframe ASCII-alphanumeric boundary is NOT relaxed.

P1-2 - the R2 unsupported-timeframe guard sat BEFORE branch dispatch, so it
blocked EVERY symbol intent. The contract: only the period-consuming intents
(``analyze_once``, ``create_paper_order``, ``add_symbol``) reject on an
unsupported timeframe (no analyze, no persist); ``pause_symbol``,
``resume_symbol``, ``remove_symbol`` do NOT consume periods and must proceed
to their control functions exactly once even when ``3m`` appears in the text;
early-return intents (``system_status`` etc.) stay unchanged.

Pure parser + mocked-handler tests: no live PG/network access; marks mirror
the R2 companion file for suite partitioning.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

import unittest
from unittest.mock import patch

from plugins.crypto_guard.notify.intent_parser import parse_intent
from plugins.crypto_guard.tools import ga_crypto_tools
from plugins.crypto_guard.tools.ga_crypto_tools import crypto_handle_text_command


class TestWordInternalTimeframeRejectionP1(unittest.TestCase):
    """P1-1: timeframe tokens inside English words must NOT be recognized.

    RED-first: on the R2 numeric-only boundary, every case below wrongly
    surfaces a timeframe (``web3market``->unsupported ``3m``, ``v1high``->
    ``1h``, ``x15months``->``15m``, ``alpha5model``->``5m``,
    ``version4high``->``4h``), so the ``[]`` assertions FAIL.
    """

    WORD_CASES = (
        "web3market",
        "v1high",
        "x15months",
        "alpha5model",
        "version4high",
    )

    def test_word_internal_sequences_yield_no_timeframes(self) -> None:
        for word in self.WORD_CASES:
            with self.subTest(word=word):
                intent = parse_intent(f"分析 {word} 有没有机会")
                self.assertEqual(intent.get("timeframes"), [], intent)
                self.assertEqual(intent.get("display_timeframes"), [], intent)
                self.assertEqual(intent.get("unsupported_timeframes"), [], intent)

    def test_word_internal_sequences_bare_text(self) -> None:
        for word in self.WORD_CASES:
            with self.subTest(word=word):
                intent = parse_intent(word)
                self.assertEqual(intent.get("timeframes"), [], intent)
                self.assertEqual(intent.get("unsupported_timeframes"), [], intent)

    def test_word_internal_sequences_do_not_pollute_symbol_request(self) -> None:
        """A real symbol plus a wordy token: symbol wins, no phantom TF."""
        intent = parse_intent("分析 BTCUSDT web3market")
        self.assertEqual(intent.get("symbol"), "BTCUSDT", intent)
        self.assertEqual(intent.get("timeframes"), [], intent)
        self.assertEqual(intent.get("unsupported_timeframes"), [], intent)

    def test_cjk_adjacency_still_recognized(self) -> None:
        """CJK chars are not ASCII alphanumerics: boundary must pass."""
        self.assertEqual(parse_intent("看一下15m的BTCUSDT")["timeframes"], ["15m"])
        self.assertEqual(parse_intent("看一下3m的BTCUSDT")["unsupported_timeframes"],
                         ["3m"])

    def test_15m_still_does_not_surface_5m(self) -> None:
        intent = parse_intent("分析 BTCUSDT 15m")
        self.assertEqual(intent.get("timeframes"), ["15m"], intent)

    def test_order_dedup_case_preserved(self) -> None:
        intent = parse_intent("分析 BTCUSDT 5M 15m 5m 1D")
        self.assertEqual(intent.get("timeframes"), ["5m", "15m", "1d"], intent)


class TestSymbolTimeframeCombinedParseP2_1(unittest.TestCase):
    """P2-1 option A: no-separator ``<symbol>USDT<tf>`` combined parse must
    yield BOTH the symbol and the timeframe.

    RED-first: on current code ``parse_intent("分析BTCUSDT15m机会")`` returns
    ``symbol=None`` (SYMBOL_RE ``\\b`` failure), so the symbol assertions and
    the real-handler dispatch test FAIL.
    """

    def test_combined_symbol_and_timeframe(self) -> None:
        intent = parse_intent("分析BTCUSDT15m机会")
        self.assertEqual(intent.get("symbol"), "BTCUSDT", intent)
        self.assertEqual(intent.get("timeframes"), ["15m"], intent)
        self.assertEqual(intent.get("unsupported_timeframes"), [], intent)

    def test_combined_5m_variant(self) -> None:
        intent = parse_intent("分析BTCUSDT5m")
        self.assertEqual(intent.get("symbol"), "BTCUSDT", intent)
        self.assertEqual(intent.get("timeframes"), ["5m"], intent)

    def test_combined_case_insensitive(self) -> None:
        intent = parse_intent("分析btcusdt15M机会")
        self.assertEqual(intent.get("symbol"), "BTCUSDT", intent)
        self.assertEqual(intent.get("timeframes"), ["15m"], intent)

    def test_combined_unsupported_timeframe(self) -> None:
        """``BTCUSDT3m``: symbol recognized, 3m recorded as unsupported."""
        intent = parse_intent("分析BTCUSDT3m")
        self.assertEqual(intent.get("symbol"), "BTCUSDT", intent)
        self.assertEqual(intent.get("timeframes"), [], intent)
        self.assertEqual(intent.get("unsupported_timeframes"), ["3m"], intent)

    def test_first_symbol_position_wins_over_combined(self) -> None:
        """Positional first-symbol contract: an earlier standalone symbol wins;
        the combined token still contributes its timeframe."""
        intent = parse_intent("分析 ETHUSDT 和 BTCUSDT15m")
        self.assertEqual(intent.get("symbol"), "ETHUSDT", intent)
        self.assertEqual(intent.get("timeframes"), ["15m"], intent)

    def test_real_handler_dispatches_combined_symbol_and_timeframes(self) -> None:
        """Real consumption path: ``crypto_handle_text_command`` must dispatch
        ``crypto_analyze_symbol_once`` exactly once with symbol ``BTCUSDT``
        and display timeframes ``["15m"]`` for ``分析BTCUSDT15m机会``."""
        with patch.object(
            ga_crypto_tools, "crypto_analyze_symbol_once",
            return_value={"ok": True, "symbol": "BTCUSDT", "signal_id": 1},
        ) as mock_analyze:
            result = crypto_handle_text_command("分析BTCUSDT15m机会",
                                                user_id="test_user")
        self.assertTrue(mock_analyze.called,
                        f"analyze not dispatched; result={result}")
        self.assertEqual(mock_analyze.call_count, 1)
        args, kwargs = mock_analyze.call_args
        self.assertEqual(args[0], "BTCUSDT", (args, kwargs))
        self.assertEqual(args[1], ["15m"], (args, kwargs))
        self.assertTrue(result.get("ok"), result)

    # 终审返工 R3 P1-1 entity-resolution (2026-07-26, second blocker batch):
    # the leading-slash command ``/analyzeBTCUSDT15m`` and the CJK-adjacent
    # ``看一下15m的BTCUSDT`` writings must resolve the SAME way as the CJK
    # no-separator ``分析BTCUSDT15m机会`` case: symbol ``BTCUSDT`` + ``15m``.
    # RED-first on the current ``\\b`` SYMBOL_RE: ``/analyzeBTCUSDT15m`` yields
    # ``ANALYZEBTCUSDT`` (the ``\\b`` treats ``/analyze`` as a word boundary so
    # ``ANALYZEBTCUSDT`` is one ``\\b``-bounded token; the ``ANALYZE`` skip never
    # fires because the skip set is checked against the FULL token
    # ``ANALYZEBTCUSDT``, not its prefix); ``看一下15m的BTCUSDT`` yields
    # ``symbol=None`` (``\\b`` cannot anchor between CJK and ``BTCUSDT`` because
    # CJK is ``\\w`` under Python's Unicode word-char semantics).

    def test_leading_slash_command_no_separator(self) -> None:
        """``/analyzeBTCUSDT15m`` -> symbol BTCUSDT, timeframes ['15m'].

        RED-first: current ``\\b`` SYMBOL_RE returns ``ANALYZEBTCUSDT``.
        """
        intent = parse_intent("/analyzeBTCUSDT15m")
        self.assertEqual(intent.get("symbol"), "BTCUSDT", intent)
        self.assertEqual(intent.get("timeframes"), ["15m"], intent)
        self.assertEqual(intent.get("intent"), "analyze_once", intent)
        self.assertNotIn("ANALYZE", intent.get("symbol") or "", intent)

    def test_cjk_adjacent_symbol_recognized(self) -> None:
        """``看一下15m的BTCUSDT`` -> symbol BTCUSDT, timeframes ['15m'].

        RED-first: current ``\\b`` SYMBOL_RE returns ``None`` (no ``\\b``
        between CJK ``的`` and ``B``), so symbol=None FAILS the assertion.
        """
        intent = parse_intent("看一下15m的BTCUSDT")
        self.assertEqual(intent.get("symbol"), "BTCUSDT", intent)
        self.assertEqual(intent.get("timeframes"), ["15m"], intent)

    def test_existing_combined_case_still_works(self) -> None:
        """Regression guard: the already-passing CJK no-separator case must
        keep returning symbol BTCUSDT + timeframes ['15m'] after the fix."""
        intent = parse_intent("分析BTCUSDT15m机会")
        self.assertEqual(intent.get("symbol"), "BTCUSDT", intent)
        self.assertEqual(intent.get("timeframes"), ["15m"], intent)

    def test_normal_spaced_input_still_works(self) -> None:
        """Regression guard: the plain spaced writing keeps working."""
        intent = parse_intent("分析 BTCUSDT 15m")
        self.assertEqual(intent.get("symbol"), "BTCUSDT", intent)
        self.assertEqual(intent.get("timeframes"), ["15m"], intent)

    def test_command_word_inside_word_not_symbol(self) -> None:
        """A command word glued to a symbol must NOT let the command word
        leak into the symbol. ``/analyzeBTCUSDT`` (no TF) -> BTCUSDT, never
        ``ANALYZEBTCUSDT``; ``statusBTCUSDT`` -> BTCUSDT, never ``STATUSBTCUSDT``.
        """
        intent = parse_intent("/analyzeBTCUSDT")
        self.assertEqual(intent.get("symbol"), "BTCUSDT", intent)
        self.assertNotIn("ANALYZE", intent.get("symbol") or "", intent)
        intent2 = parse_intent("statusBTCUSDT")
        self.assertEqual(intent2.get("symbol"), "BTCUSDT", intent2)
        self.assertNotIn("STATUS", intent2.get("symbol") or "", intent2)

    def test_symbol_re_comment_names_real_strip_mechanism_not_phantom_helper(self) -> None:
        """Reviewer P2 (2026-07-26, R3 second blocker batch): the ``SYMBOL_RE``
        comment block MUST point at the REAL strip mechanism, not a phantom
        ``_strip_leading_command`` helper that does not exist anywhere in the
        codebase.

        Mirrors the R2 ``test_timeframes_docstring_matches_compiled_regex``
        pin: that test forbids the ``_timeframes`` docstring from drifting to
        the old numeric-only lookaround; this forbids the ``SYMBOL_RE``
        comment from naming a non-existent ``_strip_leading_command``
        function. The actual mechanism is the inline
        ``_LEADING_COMMAND_RE.sub("", text, count=1)`` call inside
        ``_first_symbol``; a maintainer searching for the named helper must
        find the real call site, not a missing symbol.

        RED-first on the pre-fix comment: the comment read
        ``see ``_strip_leading_command`` in ``_first_symbol````, so the
        ``assertNotIn("_strip_leading_command", src)`` assertion FAILED.
        """
        import inspect
        from plugins.crypto_guard.notify import intent_parser
        src = inspect.getsource(intent_parser)
        # The real mechanism is present in _first_symbol.
        self.assertIn("_LEADING_COMMAND_RE.sub", src,
                      "_first_symbol must strip the leading command via "
                      "_LEADING_COMMAND_RE.sub (the real mechanism)")
        # The comment must NOT name a phantom _strip_leading_command helper.
        self.assertNotIn("_strip_leading_command", src,
                         "the SYMBOL_RE comment references a non-existent "
                         "_strip_leading_command helper; point the comment at "
                         "_LEADING_COMMAND_RE.sub in _first_symbol instead")

    def test_real_handler_leading_slash_dispatches_btcusdt_not_analyzebtcusdt(self) -> None:
        """Real consumption path: ``/analyzeBTCUSDT15m`` must dispatch
        ``crypto_analyze_symbol_once`` exactly once with ``BTCUSDT`` (NOT
        ``ANALYZEBTCUSDT``) and display timeframes ``["15m"]``.

        RED-first: current ``\\b`` SYMBOL_RE -> handler dispatches with
        ``ANALYZEBTCUSDT`` -> the ``args[0]=="BTCUSDT"`` assertion FAILS.
        """
        with patch.object(
            ga_crypto_tools, "crypto_analyze_symbol_once",
            return_value={"ok": True, "symbol": "BTCUSDT", "signal_id": 1},
        ) as mock_analyze:
            result = crypto_handle_text_command("/analyzeBTCUSDT15m",
                                                user_id="test_user")
        self.assertTrue(mock_analyze.called,
                        f"analyze not dispatched; result={result}")
        self.assertEqual(mock_analyze.call_count, 1)
        args, kwargs = mock_analyze.call_args
        self.assertEqual(args[0], "BTCUSDT", (args, kwargs))
        self.assertNotEqual(args[0], "ANALYZEBTCUSDT", (args, kwargs))
        self.assertEqual(args[1], ["15m"], (args, kwargs))
        self.assertTrue(result.get("ok"), result)


class TestUnsupportedGuardScopedToPeriodConsumingIntentsP1_2(unittest.TestCase):
    """P1-2: the unsupported-timeframe guard lives INSIDE the period-consuming
    intent branches only.

    RED-first: on the R2 code the guard sits before branch dispatch, so the
    pause/resume/remove tests below FAIL (the control function is never
    called; the handler returns the unsupported_timeframe error instead).
    """

    def test_pause_symbol_with_3m_is_not_blocked(self) -> None:
        with patch.object(
            ga_crypto_tools, "crypto_symbol_pause",
            return_value={"ok": True, "symbol": "BTCUSDT", "status": "paused"},
        ) as mock_pause:
            result = crypto_handle_text_command("暂停 BTCUSDT 3m 分析",
                                                user_id="test_user")
        self.assertEqual(mock_pause.call_count, 1,
                         f"pause must run despite 3m; result={result}")
        self.assertEqual(mock_pause.call_args[0][0], "BTCUSDT")
        self.assertTrue(result.get("ok"), result)

    def test_resume_symbol_with_3m_is_not_blocked(self) -> None:
        with patch.object(
            ga_crypto_tools, "crypto_symbol_resume",
            return_value={"ok": True, "symbol": "BTCUSDT", "status": "active"},
        ) as mock_resume:
            result = crypto_handle_text_command("恢复 BTCUSDT 3m 分析",
                                                user_id="test_user")
        self.assertEqual(mock_resume.call_count, 1,
                         f"resume must run despite 3m; result={result}")
        self.assertEqual(mock_resume.call_args[0][0], "BTCUSDT")
        self.assertTrue(result.get("ok"), result)

    def test_remove_symbol_with_3m_is_not_blocked(self) -> None:
        with patch.object(
            ga_crypto_tools, "crypto_symbol_remove",
            return_value={"ok": True, "symbol": "BTCUSDT", "status": "removed"},
        ) as mock_remove:
            result = crypto_handle_text_command("移除 BTCUSDT 3m",
                                                user_id="test_user")
        self.assertEqual(mock_remove.call_count, 1,
                         f"remove must run despite 3m; result={result}")
        self.assertEqual(mock_remove.call_args[0][0], "BTCUSDT")
        self.assertTrue(result.get("ok"), result)

    def test_add_symbol_with_3m_is_rejected_without_persist(self) -> None:
        with patch.object(
            ga_crypto_tools, "crypto_symbol_add",
            side_effect=AssertionError(
                "crypto_symbol_add must NOT run for an unsupported-timeframe "
                "add request"),
        ) as mock_add:
            result = crypto_handle_text_command("BTCUSDT 3m 加入监控",
                                                user_id="test_user")
            self.assertFalse(mock_add.called)
        self.assertFalse(result.get("ok", True), result)
        self.assertEqual(result.get("error"), "unsupported_timeframe", result)
        self.assertEqual(result.get("unsupported_timeframes"), ["3m"], result)
        self.assertIn("3m", result.get("text") or "")
        self.assertIn("不支持", result.get("text") or "")

    def test_create_paper_order_with_3m_is_rejected_without_analyze(self) -> None:
        with patch.object(
            ga_crypto_tools, "crypto_analyze_symbol_once",
            side_effect=AssertionError(
                "crypto_analyze_symbol_once must NOT run for an "
                "unsupported-timeframe paper-order request"),
        ) as mock_analyze, patch.object(
            ga_crypto_tools, "crypto_create_paper_order_from_signal",
            side_effect=AssertionError(
                "crypto_create_paper_order_from_signal must NOT run for an "
                "unsupported-timeframe paper-order request"),
        ) as mock_order:
            result = crypto_handle_text_command("BTCUSDT 3m 开模拟",
                                                user_id="test_user")
            self.assertFalse(mock_analyze.called)
            self.assertFalse(mock_order.called)
        self.assertFalse(result.get("ok", True), result)
        self.assertEqual(result.get("error"), "unsupported_timeframe", result)
        self.assertEqual(result.get("unsupported_timeframes"), ["3m"], result)
        self.assertIn("3m", result.get("text") or "")

    def test_analyze_once_with_3m_still_rejected(self) -> None:
        """The R2 analyze_once rejection contract survives the guard move."""
        with patch.object(
            ga_crypto_tools, "crypto_analyze_symbol_once",
            side_effect=AssertionError(
                "crypto_analyze_symbol_once must NOT run for an "
                "unsupported-timeframe analyze request"),
        ) as mock_analyze:
            result = crypto_handle_text_command("分析 BTCUSDT 3m",
                                                user_id="test_user")
            self.assertFalse(mock_analyze.called)
        self.assertFalse(result.get("ok", True), result)
        self.assertEqual(result.get("unsupported_timeframes"), ["3m"], result)

    def test_system_status_early_return_unchanged_with_3m(self) -> None:
        """Early-return intents stay unchanged even when 3m appears."""
        with patch.object(
            ga_crypto_tools, "crypto_system_status",
            return_value={"ok": True, "status": "healthy"},
        ) as mock_status:
            result = crypto_handle_text_command("系统状态 3m", user_id="test_user")
        self.assertEqual(mock_status.call_count, 1, result)
        self.assertTrue(result.get("ok"), result)
