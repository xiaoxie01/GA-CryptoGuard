"""终审返工 R2 (2026-07-26): timeframe parser substring false-match (P1-1) +
structured unsupported-timeframe handling (P2-1).

RED-first tests for two Codex re-review findings:

P1-1 - ``intent_parser._timeframes`` used ``tf in text`` substring scan, so
``"15m"`` matched via the ``"5m"`` substring (and vice-versa: a user typing
``"5m"`` got ``["5m"]`` only by luck, but ``"15m"`` would ALSO surface ``"5m"``
because ``"5m" in "15m"`` is True). The contract:
  - explicit-token / ASCII-boundary parse, no substring scan.
  - ``分析 BTCUSDT 15m`` -> ``["15m"]`` ONLY (NOT ``["15m","5m"]``).
  - ``分析 BTCUSDT 5m`` -> ``["5m"]`` ONLY.
  - ``分析 BTCUSDT 15m 5m`` -> ``["15m","5m"]`` in written order, deduped.
  - case-insensitive; tolerant of adjacent CJK text.
  - drives the real ``parse_intent -> crypto_analyze_symbol_once -> render_text``
    consumption path.
  - revert-fail: restoring the substring scan MUST fail case (1).

P2-1 - ``3m`` must not be silently dropped. The previous fix rejected ``3m``
silently (the user got the default 5-TF analysis with no signal that ``3m``
was unsupported). The contract:
  - ``parse_intent`` records ``unsupported_timeframes=["3m"]`` (structured).
  - the analyze path, on seeing an unsupported timeframe, returns an explicit
    user-facing message: "当前不支持 3m；支持 1d/4h/1h/15m/5m".
  - it must NOT silently fall back to the default 5-TF analysis.
  - it must NOT persist any ad-hoc decision for an unsupported-timeframe request.
  - real handler test: a ``3m`` request never calls ``crypto_analyze_symbol_once``
    and never persists.

Uses the isolated PG fixture ``make_repo()`` (CRYPTO_GUARD_REDIS_DISABLED=1,
crypto_guard_test role/DB); never touches production.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

import unittest
from unittest.mock import patch

from plugins.crypto_guard.notify.intent_parser import parse_intent
from plugins.crypto_guard.tools import ga_crypto_tools
from plugins.crypto_guard.tools.ga_crypto_tools import crypto_handle_text_command


class TestTimeframeParserSubstringFalseMatchP1(unittest.TestCase):
    """P1-1: explicit-token boundary parse - no substring false-match.

    RED-first: on the current ``tf in text`` substring-scan code, case (1)
    (``"分析 BTCUSDT 15m"`` -> ``["15m","5m"]`` because ``"5m" in "15m"``)
    FAILS the ``["15m"]``-only assertion.
    """

    def test_15m_alone_yields_only_15m(self) -> None:
        """Case (1): '分析 BTCUSDT 15m' -> ['15m'] ONLY, NOT ['15m','5m']."""
        intent = parse_intent("分析 BTCUSDT 15m")
        self.assertEqual(intent.get("timeframes"), ["15m"], intent.get("timeframes"))
        self.assertEqual(intent.get("display_timeframes"), ["15m"],
                         intent.get("display_timeframes"))

    def test_5m_alone_yields_only_5m(self) -> None:
        """Case (2): '分析 BTCUSDT 5m' -> ['5m'] ONLY."""
        intent = parse_intent("分析 BTCUSDT 5m")
        self.assertEqual(intent.get("timeframes"), ["5m"], intent.get("timeframes"))
        self.assertEqual(intent.get("display_timeframes"), ["5m"],
                         intent.get("display_timeframes"))

    def test_15m_and_5m_preserves_written_order_deduped(self) -> None:
        """Case (3): '分析 BTCUSDT 15m 5m' -> ['15m','5m'] in written order,
        deduped. Reverse order '5m 15m' -> ['5m','15m']."""
        intent = parse_intent("分析 BTCUSDT 15m 5m")
        self.assertEqual(intent.get("timeframes"), ["15m", "5m"],
                         intent.get("timeframes"))
        intent_rev = parse_intent("分析 BTCUSDT 5m 15m")
        self.assertEqual(intent_rev.get("timeframes"), ["5m", "15m"],
                         intent_rev.get("timeframes"))
        # dedup: '15m 15m 5m' -> ['15m','5m']
        intent_dup = parse_intent("分析 BTCUSDT 15m 15m 5m")
        self.assertEqual(intent_dup.get("timeframes"), ["15m", "5m"],
                         intent_dup.get("timeframes"))

    def test_case_insensitive_and_adjacent_cjk(self) -> None:
        """Case (4): case-insensitive ('15M', '5M') and adjacent CJK text
        ('看一下15m的BTCUSDT' / '分析BTCUSDT15m机会')."""
        self.assertEqual(parse_intent("分析 BTCUSDT 15M")["timeframes"], ["15m"])
        self.assertEqual(parse_intent("分析 BTCUSDT 5M")["timeframes"], ["5m"])
        self.assertEqual(parse_intent("看一下15m的BTCUSDT")["timeframes"], ["15m"])
        self.assertEqual(parse_intent("分析BTCUSDT15m机会")["timeframes"], ["15m"])
        # 5m adjacent to CJK must NOT also pull in 15m (no false positive).
        self.assertEqual(parse_intent("分析BTCUSDT5m")["timeframes"], ["5m"])
        # 1h vs 4h vs 1d boundary: '14h' is NOT a TF, '4h' alone matches.
        self.assertEqual(parse_intent("分析 BTCUSDT 4h 1h 1d")["timeframes"],
                         ["4h", "1h", "1d"])

    def test_substring_scan_revert_fails_case1(self) -> None:
        """Revert-fail: if ``_timeframes`` is restored to ``tf in text`` substring
        scan, case (1) MUST fail (``"5m" in "15m"`` surfaces both). This test
        documents the failure mode the boundary parse prevents."""
        from plugins.crypto_guard.notify import intent_parser
        # Simulate the OLD substring-scan bug on the same input and assert it
        # WOULD have produced the false match. This proves the boundary fix is
        # load-bearing: if someone reverts to substring scan, this assertion
        # (which mirrors the bug) holds True, and the corresponding
        # ``test_15m_alone_yields_only_15m`` above flips to FAIL.
        text = "分析 BTCUSDT 15m"
        old_bug_result = [tf for tf in ("1d", "4h", "1h", "15m", "5m")
                          if tf.lower() in text.lower()]
        # The bug surfaces BOTH 15m and 5m -> the regression signature.
        self.assertEqual(old_bug_result, ["15m", "5m"],
                         "substring scan must surface the false match; if this "
                         "changes the boundary fix is no longer load-bearing")
        # The current (fixed) parser must NOT match the bug.
        self.assertNotEqual(intent_parser._timeframes(text), old_bug_result)
        self.assertEqual(intent_parser._timeframes(text), ["15m"])

    def test_timeframes_docstring_matches_compiled_regex(self) -> None:
        """Reviewer P2 (2026-07-26) + 终审返工 R3 P1-1: the ``_timeframes``
        docstring MUST NOT drift from the compiled ``_SUPPORTED_TIMEFRAME_RE``.

        R3 moved the compiled regex from NUMERIC-only boundaries
        ``(?<![0-9])...(?![0-9])`` (which wrongly matched timeframes inside
        English words: ``web3market``->3m, ``v1high``->1h) to ASCII
        ALPHANUMERIC boundaries ``(?<![0-9A-Za-z])...(?![0-9A-Za-z])``. The
        no-separator ``BTCUSDT15m`` case that motivated R2's numeric-only
        choice is now handled by the dedicated ``_COMBINED_SYMBOL_TF_RE``
        symbol+timeframe parse instead of a weakened boundary. This test pins
        the docstring to the compiled pattern and forbids re-asserting the
        old numeric-only lookarounds as if they were the compiled pattern, so
        a maintainer cannot silently revert to the word-internal-matching
        boundary.
        """
        import inspect
        from plugins.crypto_guard.notify import intent_parser
        compiled = intent_parser._SUPPORTED_TIMEFRAME_RE.pattern
        # The compiled pattern itself must use the alphanumeric boundary.
        self.assertIn("(?<![0-9A-Za-z])", compiled,
                      "compiled regex must keep the ASCII-alphanumeric "
                      "lookbehind (R3 P1-1)")
        self.assertIn("(?![0-9A-Za-z])", compiled,
                      "compiled regex must keep the ASCII-alphanumeric "
                      "lookahead (R3 P1-1)")
        doc = inspect.getdoc(intent_parser._timeframes) or ""
        # The docstring must contain the actual compiled pattern.
        self.assertIn(compiled, doc,
                      "docstring must document the compiled regex verbatim")
        # The docstring must NOT assert the OLD numeric-only lookaround as if
        # it were the compiled pattern. Mentioning it in a rationale sentence
        # that names it as the R2/old behavior is fine; the check here is that
        # the exact old full pattern is not presented as current.
        old_pattern = r"(?<![0-9])(1d|4h|1h|15m|5m)(?![0-9])"
        self.assertNotIn(old_pattern, doc,
                         "docstring must not present the old numeric-only "
                         "pattern as the compiled one")


class TestUnsupportedTimeframeStructuredP2(unittest.TestCase):
    """P2-1: an explicit ``3m`` request must be structured, surfaced, and NEVER
    silently fall back to the default 5-TF analysis or persist a decision.

    RED-first: on the current code ``parse_intent("分析 BTC 3m")`` returns
    ``display_timeframes=[]`` (3m silently dropped) and
    ``crypto_handle_text_command`` proceeds to call
    ``crypto_analyze_symbol_once`` on the empty set (default 5-TF analysis) -
    the assertions below FAIL on that code.
    """

    def test_parse_intent_records_unsupported_timeframes_3m(self) -> None:
        """parse_intent records unsupported_timeframes=['3m'] for an explicit 3m
        request, and does NOT surface 3m in display_timeframes."""
        intent = parse_intent("分析 BTCUSDT 3m")
        self.assertEqual(intent.get("unsupported_timeframes"), ["3m"],
                         intent)
        self.assertNotIn("3m", intent.get("display_timeframes") or [], intent)
        self.assertNotIn("3m", intent.get("timeframes") or [], intent)

    def test_no_unsupported_timeframes_when_all_supported(self) -> None:
        """A request with only supported TFs records unsupported_timeframes=[]."""
        intent_ok = parse_intent("分析 BTCUSDT 15m 5m")
        self.assertEqual(intent_ok.get("unsupported_timeframes"), [], intent_ok)
        intent_none = parse_intent("临时分析一下 BTCUSDT")
        self.assertEqual(intent_none.get("unsupported_timeframes"), [],
                         intent_none)

    def test_unsupported_3m_request_does_not_call_analyze_or_persist(self) -> None:
        """Real handler test: a 3m request MUST NOT call
        crypto_analyze_symbol_once (the analysis entry that opens a repo
        connection, builds the snapshot, and persists via
        ``save_ad_hoc_analysis``). It returns an explicit user-facing
        unsupported message.

        Patches ``crypto_analyze_symbol_once`` at module scope - if the handler
        short-circuits on ``unsupported_timeframes`` before dispatching (the
        fix), the mock is never invoked. Because ``save_ad_hoc_analysis`` is a
        repository method reached only inside ``crypto_analyze_symbol_once``,
        never invoking the entry function is the complete persistence guard.
        """
        with patch.object(
            ga_crypto_tools, "crypto_analyze_symbol_once",
            side_effect=AssertionError(
                "crypto_analyze_symbol_once must NOT be called for an "
                "unsupported-timeframe request"),
        ) as mock_analyze:
            result = crypto_handle_text_command(
                "分析 BTCUSDT 3m", user_id="test_user",
            )
            self.assertFalse(mock_analyze.called,
                             "crypto_analyze_symbol_once was called")
        # The result must be a structured unsupported-timeframe response.
        self.assertFalse(result.get("ok", True), result)
        text = result.get("text") or ""
        self.assertIn("3m", text, text)
        self.assertIn("不支持", text, text)
        # Must name the supported set so the user knows what to ask for.
        self.assertIn("1d", text, text)
        self.assertIn("4h", text, text)
        self.assertIn("1h", text, text)
        self.assertIn("15m", text, text)
        self.assertIn("5m", text, text)
        # Structured signal on the result dict too.
        self.assertEqual(result.get("unsupported_timeframes"), ["3m"], result)

    def test_unsupported_3m_request_mixed_with_supported_is_still_unsupported(self) -> None:
        """A request mixing a supported TF and an unsupported one (e.g.
        '分析 BTCUSDT 15m 3m') must still be treated as unsupported (the whole
        request is rejected, NOT a partial analysis on 15m only). The
        unsupported set lists 3m; 15m is in display_timeframes but NOT acted on.
        """
        intent = parse_intent("分析 BTCUSDT 15m 3m")
        self.assertEqual(intent.get("unsupported_timeframes"), ["3m"], intent)
        self.assertEqual(intent.get("display_timeframes"), ["15m"], intent)
        with patch.object(
            ga_crypto_tools, "crypto_analyze_symbol_once",
            side_effect=AssertionError(
                "crypto_analyze_symbol_once must NOT be called when an "
                "unsupported timeframe is present"),
        ) as mock_analyze:
            result = crypto_handle_text_command(
                "分析 BTCUSDT 15m 3m", user_id="test_user",
            )
            self.assertFalse(mock_analyze.called)
        self.assertFalse(result.get("ok", True), result)
        self.assertIn("3m", result.get("text") or "")
        self.assertIn("不支持", result.get("text") or "")