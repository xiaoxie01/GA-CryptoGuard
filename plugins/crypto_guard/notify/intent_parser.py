from __future__ import annotations

import re
from typing import Any

from plugins.crypto_guard.data.binance_rest import normalize_symbol


# Standalone symbol token. 终审返工 R3 P1-1 entity-resolution (2026-07-26,
# second blocker batch): the original ``\b([A-Za-z]{2,12}(?:USDT)?)\b`` relied
# on Python's ``\b`` / ``\w`` Unicode semantics, which broke in TWO real
# writings the combined parse did not cover:
#   1. ``看一下15m的BTCUSDT`` -> symbol=None. ``\b`` cannot anchor between a
#      CJK char and ``B`` because CJK chars ARE ``\w``, so there is no word
#      boundary between ``的`` and ``B``. The standalone ``BTCUSDT`` tail is
#      invisible to ``SYMBOL_RE``.
#   2. ``/analyzeBTCUSDT15m`` -> symbol="ANALYZEBTCUSDT". ``\b`` treats the
#      ``/`` before ``analyze`` as a word boundary, so ``ANALYZEBTCUSDT`` is
#      one ``\b``-bounded token (the ``ANALYZE`` command-word skip never fires
#      because the skip set is checked against the FULL token, not a prefix).
# Fix: use the SAME ASCII-alphanumeric boundary discipline as the timeframe
# regexes - ``(?<![0-9A-Za-z])...(?![0-9A-Za-z])``. CJK chars are NOT ASCII
# alphanumerics, so the boundary passes at a CJK/ASCII junction (case 1 fixed);
# a leading slash command is stripped BEFORE the entity scan (case 2 fixed -
# see the ``_LEADING_COMMAND_RE.sub`` call in ``_first_symbol``), so ``ANALYZE`` never
# enters the candidate stream. This is NOT a relaxation: the boundary is still
# strict ASCII-alphanumeric, and the combined parse (``_COMBINED_SYMBOL_TF_RE``)
# still requires the ``USDT`` quote suffix as its structural discriminator.
# The optional ``USDT`` suffix here is the original behavior (a bare ``BTC``
# is accepted and ``normalize_symbol`` appends ``USDT``).
SYMBOL_RE = re.compile(
    r"(?<![0-9A-Za-z])([A-Za-z]{2,12}(?:USDT)?)(?![0-9A-Za-z])",
)
# Leading command words that may be glued to a symbol+timeframe with NO
# separator (``/analyzeBTCUSDT15m`` or ``statusBTCUSDT``). These are the same
# words the intent classifier treats as commands. They are stripped from the
# START of the text ONLY, and ONLY when immediately followed by a letter (a
# real ``/analyze BTCUSDT`` or ``status BTCUSDT`` with a space is unaffected -
# the strip regex requires the command to be glued to a letter). The entity
# scan runs on the stripped text while intent classification in ``parse_intent``
# runs on the ORIGINAL raw text, so ``/analyze`` and bare ``status`` are still
# classified as their commands. This keeps command words out of the symbol
# candidate stream without weakening any regex: a bare ``status`` (followed by
# end-of-string or a space) is NOT stripped and is NOT a symbol candidate
# anyway (it is in the ``skip`` set).
_LEADING_COMMAND_RE = re.compile(
    r"^(?:/)?(?:analyze|status|errors?|watchlist|strategies|review|latest|system|log)(?=[A-Za-z])",
    re.IGNORECASE,
)

# 终审返工 R3 P1-1 (2026-07-26): a timeframe must be a REAL token, bounded by
# ASCII alphanumerics ``(?<![0-9A-Za-z]) ... (?![0-9A-Za-z])``. The R2
# numeric-only boundary ``(?<![0-9])...(?![0-9])`` wrongly matched timeframe
# substrings INSIDE English words: ``web3market``->3m, ``v1high``->1h,
# ``x15months``->15m, ``alpha5model``->5m, ``version4high``->4h. The ASCII
# alphanumeric boundary blocks every word-internal case while keeping
# CJK-adjacent tokens recognized (``看一下15m`` - CJK chars are not ASCII
# alphanumerics, so the boundary passes). The numeric nesting trap stays
# blocked (``5m`` inside ``15m``: preceded by ``1``, an alnum). The
# no-separator ``BTCUSDT15m`` writing is deliberately NOT handled here - a
# letter-preceded timeframe fails this boundary by design. It is supported
# ONLY through the explicit symbol+timeframe combined parse
# ``_COMBINED_SYMBOL_TF_RE`` below (P2-1 option A), which requires the USDT
# quote suffix as a structural discriminator and also yields the symbol.
# Do NOT relax the letter boundary here to "fix" no-separator input - that
# reintroduces the word-internal false matches wholesale.
# Order is preserved by scanning left-to-right and de-duplicating on first
# occurrence; case-insensitive via ``IGNORECASE``. ``3m`` is deliberately NOT
# in the supported set - it is captured separately by
# ``_UNSUPPORTED_TIMEFRAME_RE`` and recorded as ``unsupported_timeframes``.
_SUPPORTED_TIMEFRAME_RE = re.compile(
    r"(?<![0-9A-Za-z])(1d|4h|1h|15m|5m)(?![0-9A-Za-z])", re.IGNORECASE,
)
# ``3m`` is the one currently-unsupported timeframe the system cannot healthily
# analyze (no ``required_samples`` entry -> default 200, not in
# ``DEFAULT_TIMEFRAMES``, scheduler never pre-seeds, ad-hoc fetch uses
# ``lookback=160`` with no backfill). It is surfaced to the user as
# ``unsupported_timeframes`` so the rejection is visible, not silent (P2-1).
# Same ASCII-alphanumeric boundary discipline as the supported regex, so
# ``web3market`` does NOT surface ``3m`` (R3 P1-1).
_UNSUPPORTED_TIMEFRAME_RE = re.compile(
    r"(?<![0-9A-Za-z])(3m)(?![0-9A-Za-z])", re.IGNORECASE,
)
# 终审返工 R3 P2-1 (2026-07-26, option A): explicit symbol+timeframe COMBINED
# parse for the no-separator writing ``BTCUSDT15m``. Structural discriminator:
# a letter run ending in the ``USDT`` quote suffix, DIRECTLY followed by a
# timeframe token (supported or the known-unsupported ``3m``), the whole
# compound bounded by ASCII alphanumerics. This recognizes BOTH the symbol
# (group 1) and the timeframe (group 2) - the previous ``SYMBOL_RE`` alone
# returned symbol=None for ``分析BTCUSDT15m机会`` because its ``\b`` anchors
# fail inside the CJK/ASCII and letter/digit junctions. The USDT suffix
# requirement is what keeps this parse strict: an arbitrary English word
# followed by digits+letter (``web3market``) can never match, so this is NOT
# a relaxation of the P1-1 letter boundary.
_COMBINED_SYMBOL_TF_RE = re.compile(
    r"(?<![0-9A-Za-z])([A-Za-z]{2,12}USDT)(1d|4h|1h|15m|5m|3m)(?![0-9A-Za-z])",
    re.IGNORECASE,
)


def parse_intent(text: str) -> dict[str, Any]:
    raw = text.strip()
    symbol = _first_symbol(raw)
    lowered = raw.lower()
    if lowered in {"/status", "status"}:
        intent = "system_status"
    elif lowered in {"/errors", "/error-log", "errors", "error-log"} or any(k in raw for k in ("错误日志", "最近错误", "失败任务", "错误查询")):
        intent = "list_errors"
    elif lowered in {"/watchlist", "watchlist"}:
        intent = "list_symbols"
    elif lowered in {"/strategies", "strategies"} or any(k in raw for k in ("策略版本", "策略列表", "查看策略")):
        intent = "list_strategy_versions"
    elif lowered.startswith("/analyze"):
        intent = "analyze_once"
    elif any(k in raw for k in ("每日复盘", "今日复盘", "昨日复盘", "复盘日报", "执行复盘")):
        intent = "daily_review"
    elif any(k in raw for k in ("系统状态", "运行状态", "服务状态", "定时任务状态", "队列状态", "任务状态", "健康检查")):
        intent = "system_status"
    elif any(k in raw for k in ("列出", "当前监控", "监控币种", "列表")):
        intent = "list_symbols"
    elif any(k in raw for k in ("暂停", "停止分析", "先别分析")):
        intent = "pause_symbol"
    elif any(k in raw for k in ("恢复", "继续分析", "重新分析")):
        intent = "resume_symbol"
    elif any(k in raw for k in ("移除", "删除", "取消监控")):
        intent = "remove_symbol"
    elif any(k in raw for k in ("临时", "不要加入", "不加入长期", "不加入监控")) and any(k in raw for k in ("分析", "看一下", "看看")):
        intent = "analyze_once"
    elif any(k in raw for k in ("加入监控", "长期", "以后也分析", "重点分析")) and not any(k in raw for k in ("不要加入", "不加入")):
        intent = "add_symbol"
    elif any(k in raw for k in ("历史持仓", "历史交易", "交易记录", "持仓记录", "平仓记录", "历史订单", "模拟盘记录", "模拟盘历史")):
        intent = "paper_positions"
    elif any(k in raw for k in ("模拟盘", "加入模拟", "开模拟")):
        intent = "create_paper_order"
    elif any(k in raw for k in ("盯着", "提醒", "机会监控")):
        intent = "create_opportunity_watch"
    elif any(k in raw for k in ("分析", "看一下", "看看", "能不能做", "有没有机会")):
        intent = "analyze_once"
    else:
        intent = "unknown"
    scope = "temporary" if any(k in raw for k in ("临时", "不要加入", "不加入长期")) else "long_term_watchlist"
    supported_tfs = _timeframes(raw)
    unsupported_tfs = _unsupported_timeframes(raw)
    return {
        "intent": intent,
        "symbol": symbol,
        "timeframes": supported_tfs,
        "display_timeframes": supported_tfs,
        "unsupported_timeframes": unsupported_tfs,
        "scope": scope,
        "raw_text": raw,
    }


def is_crypto_intent(text: str) -> bool:
    intent = parse_intent(text)
    return intent["intent"] != "unknown" and (intent["intent"] in {"list_symbols", "system_status", "list_errors", "daily_review", "list_strategy_versions", "paper_positions"} or intent.get("symbol"))


def _first_symbol(text: str) -> str | None:
    """First symbol by TEXT POSITION, merged from two sources:

    1. ``SYMBOL_RE`` - standalone ASCII-alphanumeric-boundary tokens
       (终审返工 R3 P1-1, 2026-07-26 second blocker batch: the original
       ``\\b``/``\\w`` Unicode boundary was replaced with
       ``(?<![0-9A-Za-z])...(?![0-9A-Za-z])`` so a STANDALONE symbol adjacent to
       CJK chars is recognized - ``看一下15m的BTCUSDT`` -> ``BTCUSDT``).
    2. ``_COMBINED_SYMBOL_TF_RE`` - the no-separator ``BTCUSDT15m`` compound
       (终审返工 R3 P2-1, 2026-07-26). ``SYMBOL_RE`` alone returns None for
       ``分析BTCUSDT15m机会``: its trailing ASCII-alphanumeric boundary fails at
       the letter/digit junction (``T``/``1`` are both ASCII alphanumerics), so
       the combined regex recognizes the symbol part explicitly.

    Leading slash commands glued to a symbol+timeframe
    (``/analyzeBTCUSDT15m``) are stripped from the START of the text ONLY
    before the standalone scan, via ``_LEADING_COMMAND_RE``, so the command
    word ``ANALYZE`` never enters the candidate stream as a symbol prefix.
    The strip is text-only here; intent classification in ``parse_intent`` runs
    on the ORIGINAL raw text, so the leading ``/analyze`` command is still
    recognized as ``analyze_once``.

    Candidates from both sources are sorted by start offset so the FIRST
    symbol the user wrote wins regardless of which regex captured it.
    """
    skip = {"ANALYZE", "STATUS", "ERRORS", "ERROR", "LOG", "WATCHLIST", "REVIEW", "LATEST", "SYSTEM", "STRATEGIES"}
    scan_text = _LEADING_COMMAND_RE.sub("", text, count=1)
    upper = scan_text.upper()
    candidates: list[tuple[int, str]] = []
    for match in SYMBOL_RE.finditer(upper):
        token = match.group(1)
        if token in skip:
            continue
        if token in {"LONG", "SHORT", "ENTRY", "TP", "SL"}:
            continue
        candidates.append((match.start(1), token))
    for match in _COMBINED_SYMBOL_TF_RE.finditer(upper):
        candidates.append((match.start(1), match.group(1)))
    for _, token in sorted(candidates):
        try:
            return normalize_symbol(token)
        except ValueError:
            continue
    return None


_SUPPORTED_TF_SET = frozenset({"1d", "4h", "1h", "15m", "5m"})


def _scan_timeframes(text: str) -> list[str]:
    """Positionally-merged timeframe scan over BOTH sources (standalone tokens
    and the symbol+timeframe compound), lowercase, written order, deduped on
    first occurrence. Includes supported AND unsupported tokens; callers
    filter by membership in ``_SUPPORTED_TF_SET``."""
    hits: list[tuple[int, str]] = []
    for m in _SUPPORTED_TIMEFRAME_RE.finditer(text):
        hits.append((m.start(1), m.group(1).lower()))
    for m in _UNSUPPORTED_TIMEFRAME_RE.finditer(text):
        hits.append((m.start(1), m.group(1).lower()))
    for m in _COMBINED_SYMBOL_TF_RE.finditer(text):
        hits.append((m.start(2), m.group(2).lower()))
    found: list[str] = []
    for _, tf in sorted(hits):
        if tf not in found:
            found.append(tf)
    return found


def _timeframes(text: str) -> list[str]:
    """Return the SUPPORTED timeframes the user EXPLICITLY mentioned in the
    text, in the order they were written, de-duplicated on first occurrence.

    终审返工 R3 P1-1 (2026-07-26): a timeframe must be a REAL token. The
    compiled regex ``(?<![0-9A-Za-z])(1d|4h|1h|15m|5m)(?![0-9A-Za-z])``
    (case-insensitive, ``_SUPPORTED_TIMEFRAME_RE`` at module top) anchors each
    token on ASCII-ALPHANUMERIC boundaries. The R2 numeric-only boundary
    ``(?<![0-9])...(?![0-9])`` wrongly matched timeframe substrings INSIDE
    English words - ``web3market``->3m, ``v1high``->1h, ``x15months``->15m,
    ``alpha5model``->5m, ``version4high``->4h - because a letter neighbour
    passed the numeric-only anchor. The alphanumeric boundary blocks every
    word-internal case; the numeric nesting trap stays blocked too (``5m``
    inside ``15m``: preceded by ``1``). CJK-adjacent tokens still match
    (``看一下15m`` - CJK chars are not ASCII alphanumerics). Do NOT weaken
    the boundary back to numeric-only - that reintroduces the word-internal
    false matches.

    The no-separator ``BTCUSDT15m`` writing is letter-preceded, so it fails
    this boundary BY DESIGN. It is supported exclusively through the explicit
    symbol+timeframe combined parse ``_COMBINED_SYMBOL_TF_RE`` (R3 P2-1
    option A), whose USDT-quote-suffix discriminator recognizes the symbol
    AND the timeframe without relaxing the letter boundary globally. This
    function merges both sources positionally via ``_scan_timeframes`` so
    written order and first-occurrence dedup hold across them.

    P1-2 (2026-07-24): these are the user's DISPLAY timeframes only - the
    periods the user wants surfaced in the answer. They MUST NOT be used as
    the internal required context for the GA decision analysis. Return
    ``[]`` when the user named no supported period so the caller can
    distinguish "no explicit request" from "explicit 4h/1h/..."; the caller
    then falls back to the full internal default (``DEFAULT_TIMEFRAMES``) for
    analysis.

    终审返工 reviewer P1 (2026-07-25): ``3m`` is intentionally NOT in the
    supported set. The system cannot healthily analyze ``3m`` (no
    ``required_samples`` entry -> default 200, not in ``DEFAULT_TIMEFRAMES``,
    scheduler never pre-seeds, ad-hoc fetch uses ``lookback=160`` with no
    backfill). Per the P1-1 contract the system must never claim a period it
    did not actually analyze to a healthy state. Rather than silently dropping
    ``3m`` (the prior fix), it is surfaced separately as
    ``unsupported_timeframes`` via ``_unsupported_timeframes`` so the
    rejection is visible to the user and the handler can return an explicit
    "当前不支持 3m" message instead of silently falling back to a default
    5-TF analysis.
    """
    return [tf for tf in _scan_timeframes(text) if tf in _SUPPORTED_TF_SET]


def _unsupported_timeframes(text: str) -> list[str]:
    """Return the UNSUPPORTED timeframes the user EXPLICITLY mentioned, in
    written order, de-duplicated.

    终审返工 R2 P2-1 (2026-07-26): ``3m`` must not be silently dropped. The
    prior reviewer-P1 fix rejected ``3m`` from the display set silently, so a
    user who asked for ``3m`` got the default 5-TF analysis with no signal
    that ``3m`` was unsupported. This records ``3m`` structurally so the analyze
    handler (``crypto_handle_text_command``) can return an explicit
    "当前不支持 3m；支持 1d/4h/1h/15m/5m" message and NEVER silently fall back
    to the default 5-TF analysis or persist an ad-hoc decision.

    Only explicitly-named unsupported timeframes are recorded; absence is
    ``[]``. Same ASCII-alphanumeric boundary discipline as ``_timeframes``
    (R3 P1-1), so ``3m`` inside another token (``web3market``) does NOT
    falsely match; the combined ``BTCUSDT3m`` compound DOES surface ``3m``
    via ``_COMBINED_SYMBOL_TF_RE`` so the rejection stays visible for the
    no-separator writing too.
    """
    return [tf for tf in _scan_timeframes(text)
            if tf not in _SUPPORTED_TF_SET]
