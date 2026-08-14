# -*- coding: utf-8 -*-
"""08-04 contract E (E4-E8): read-only analysis tool broker + round manager.

E4: ``AnalysisToolBroker`` is a READ-ONLY evidence broker. It exposes exactly
five methods and rejects every other name — arbitrary SQL, web search, paper
order writes, config writes, and service control are forbidden by construction
(a missing attribute or an unknown ``call()`` name raises
``BrokerForbiddenError``).

E5: strict enum params (canonical timeframe set, uppercase symbol regex,
canonical regime set), a per-call soft deadline (``timeout_s``), a result size
budget (``max_result_bytes`` with recursive trim), and a per-method result
schema validated on every success.

E6: a round manager caps tool requests at ``MAX_TOOL_REQUESTS_PER_ROUND`` (3)
per round and ``MAX_ROUNDS`` (3) total.

E7/E8: ``run_analysis_rounds`` runs a single ``normal`` round for an ordinary
quote; a conflict or a watch hit adds a ``supplement`` evidence round; an order
candidate adds a ``verifier`` round that is VETO-ONLY. ``order_allowed`` ANDs
``deterministic_risk_ok`` into the verdict, so a blocked deterministic risk
gate always yields ``veto`` and ``order_allowed=False`` — the verifier can never
bypass the risk gate.

The broker reads only through a repository-like ``repo`` argument; it never
writes, never touches the network, and never controls a service. No production
DB mutation, no marker write, no service restart.

08-10 Step 6 (prd P1-4, design §6.2/§8): the risk proposal LLM may request only
ENUMERATED read-only broker methods through a structured tool-request schema.
The 08-04 ``METHODS`` set (exactly five) is frozen by its contract test, so the
two narrow risk reads (``confirmation_lifecycle_evidence`` /
``adaptive_risk_budget``) live in the separate ``RISK_READ_METHODS`` set and
are dispatched through ``call`` without touching ``METHODS``. Every supplement
round result is stamped with source/as-of/age/trust/schema metadata; stale
lifecycle evidence raises ``BrokerStaleError``; the structured tool-request
validator + ``run_risk_supplement_round`` fail closed (``wait``) on more
requests, unknown methods or budget exhaustion.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from plugins.crypto_guard.utils import utc_ms

_LOGGER = logging.getLogger("crypto_guard.tools.analysis_tool_broker")

# E4/E5/E6 constants
MAX_TOOL_REQUESTS_PER_ROUND = 3
MAX_ROUNDS = 3
DEFAULT_TIMEOUT_S = 3.0
MAX_RESULT_BYTES = 24 * 1024  # 24 KiB per result, same budget as the HTF feature pack
MAX_STRING_LEN = 500
MAX_LIST_LEN = 100

ALLOWED_TIMEFRAMES = ("1d", "4h", "1h", "15m", "5m")
ALLOWED_REGIMES = ("normal", "high_volatility", "low_volatility", "extreme", "unknown")
_ALLOWED_SIDES = ("LONG", "SHORT")
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")

# 08-10 Step 6: the enumerated read-only method set for the risk supplement
# round. Kept SEPARATE from ``AnalysisToolBroker.METHODS`` so the 08-04
# "exactly five" contract stays frozen. ``previous_round_state`` is excluded by
# design (§8: "do not add entire prior decisions").
RISK_READ_METHODS = frozenset({
    "confirmation_lifecycle_evidence",
    "adaptive_risk_budget",
    "latest_closed_market_summary",
    "deterministic_skill_evidence",
    "relevant_watch_evidence",
    "simulated_account_state",
})

# Trust label per risk read (design §3 partition matrix): deterministic
# repo/module reads are ``trusted``; skill evidence is a deterministic module
# output (``trusted``); watch records carry untrusted free text, so the whole
# method result is ``untrusted_data``.
RISK_READ_TRUST: dict[str, str] = {
    "confirmation_lifecycle_evidence": "trusted",
    "adaptive_risk_budget": "trusted",
    "latest_closed_market_summary": "trusted",
    "deterministic_skill_evidence": "trusted",
    "relevant_watch_evidence": "untrusted_data",
    "simulated_account_state": "trusted",
}

RISK_READ_SCHEMA_VERSION: dict[str, str] = {
    "confirmation_lifecycle_evidence": "confirmation_lifecycle_v1",
    "adaptive_risk_budget": "adaptive_risk_budget_v1",
    "latest_closed_market_summary": "market_summary_v1",
    "deterministic_skill_evidence": "skill_evidence_v1",
    "relevant_watch_evidence": "watch_evidence_v1",
    "simulated_account_state": "account_state_v1",
}

# Default TTL for a prior trusted confirmation event: older evidence fails
# closed (BrokerStaleError) unless the caller passes an explicit cap.
DEFAULT_LIFECYCLE_MAX_AGE_MS = 4 * 3600 * 1000  # 4 hours

# Bound for one risk supplement round (design §8: three watches / three
# counter-evidence records / one lifecycle / one budget / one plan).
MAX_RISK_TOOL_REQUESTS = 6

# E4: names that must never be reachable on the broker, either as a method
# call or as an attribute access.
FORBIDDEN_OPERATIONS = (
    "execute_sql", "web_search", "create_paper_order", "cancel_order",
    "restart_service", "stop_service", "start_service", "write_config",
    "add_symbol", "delete_symbol", "transfer_funds",
)


class BrokerForbiddenError(Exception):
    """Raised when a write/network/service-control operation is requested."""


class BrokerParamError(Exception):
    """Raised when a method argument violates an enum/schema constraint."""


class BrokerTimeoutError(Exception):
    """Raised when a single tool call exceeds its per-call soft deadline."""


class BrokerSizeBudgetError(Exception):
    """Raised when a result cannot fit the configured size budget."""


class BrokerSchemaError(Exception):
    """Raised when a method result violates its per-method schema."""


class BrokerRoundLimitError(Exception):
    """Raised when a round exceeds MAX_TOOL_REQUESTS_PER_ROUND requests."""


class BrokerStaleError(Exception):
    """Raised when a read result is older than its evidence TTL and therefore
    cannot support approval (08-10 Step 6 fail-closed contract)."""


# ── lightweight result-schema validator (pure, no jsonschema dependency) ────
_TYPECHECK: dict[str, Any] = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def _validate_against_schema(data: Any, schema: dict[str, Any]) -> list[str]:
    """Return a list of schema violations, empty when the data conforms."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["data must be an object"]
    for field in schema.get("required", []):
        if field not in data:
            errors.append(f"missing required field: {field}")
    for field, spec in schema.get("properties", {}).items():
        if field not in data:
            continue
        expected = spec.get("type")
        value = data[field]
        if isinstance(expected, list):
            ok = any(_TYPECHECK.get(t, lambda _: True)(value) for t in expected)
        else:
            ok = _TYPECHECK.get(expected, lambda _: True)(value) if expected else True
        if not ok:
            errors.append(f"{field}: expected {expected!r}, got {type(value).__name__}")
    return errors


def _trim_value(value: Any, max_str: int, max_list: int) -> Any:
    """Recursively trim strings/lists to stay inside the size budget."""
    if isinstance(value, str) and len(value) > max_str:
        return value[:max_str] + "…"
    if isinstance(value, list):
        return [_trim_value(v, max_str, max_list) for v in value[:max_list]]
    if isinstance(value, dict):
        return {k: _trim_value(v, max_str, max_list) for k, v in value.items()}
    return value


def _monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


# E5: per-method result schema (lightweight; validated on every success).
RESULT_SCHEMAS: dict[str, dict[str, Any]] = {
    "latest_closed_market_summary": {
        "required": ["symbol", "timeframe", "analysis_time_utc", "count"],
        "properties": {
            "symbol": {"type": "string"},
            "timeframe": {"type": "string"},
            "analysis_time_utc": {"type": "integer"},
            "count": {"type": "integer"},
            "latest_close_time": {"type": ["integer", "null"]},
            "last_close": {"type": ["number", "null"]},
            "last_ohlc": {"type": ["object", "null"]},
        },
    },
    "deterministic_skill_evidence": {
        "required": ["symbol", "timeframe", "analysis_time_utc", "skill_refs", "count"],
        "properties": {
            "symbol": {"type": "string"},
            "timeframe": {"type": "string"},
            "analysis_time_utc": {"type": "integer"},
            "skill_refs": {"type": "object"},
            "count": {"type": "integer"},
        },
    },
    "previous_round_state": {
        "required": ["latest"],
        "properties": {
            "latest": {"type": ["object", "null"]},
        },
    },
    "relevant_watch_evidence": {
        "required": ["symbol", "regime", "watches", "count"],
        "properties": {
            "symbol": {"type": "string"},
            "regime": {"type": "string"},
            "watches": {"type": "array"},
            "count": {"type": "integer"},
        },
    },
    "simulated_account_state": {
        "required": ["open_orders_count"],
        "properties": {
            "open_orders_count": {"type": "integer"},
            "symbols": {"type": "array"},
            "total_risk_units": {"type": ["number", "null"]},
            "concentration_breach": {"type": "boolean"},
            "risk_override_required": {"type": "boolean"},
        },
    },
    # 08-10 Step 6: narrow risk reads. Their ``data`` is self-describing —
    # source / as-of / age / trust / schema_version ride inside the result so a
    # direct ``call`` already satisfies the P1-4 metadata contract.
    "confirmation_lifecycle_evidence": {
        "required": [
            "symbol", "side", "analysis_time_utc", "status", "fingerprint",
            "direction", "close_time", "age_bars",
            "source", "as_of", "age_ms", "trust", "schema_version",
        ],
        "properties": {
            "symbol": {"type": "string"},
            "side": {"type": "string"},
            "analysis_time_utc": {"type": "integer"},
            "status": {"type": "string"},
            "fingerprint": {"type": ["string", "null"]},
            "direction": {"type": ["string", "null"]},
            "close_time": {"type": ["integer", "null"]},
            "age_bars": {"type": ["integer", "null"]},
            "source": {"type": "string"},
            "as_of": {"type": "integer"},
            "age_ms": {"type": "integer"},
            "trust": {"type": "string"},
            "schema_version": {"type": "string"},
        },
    },
    "adaptive_risk_budget": {
        "required": [
            "symbol", "as_of", "source", "age_ms", "trust", "schema_version",
            "open_orders_count",
        ],
        "properties": {
            "symbol": {"type": ["string", "null"]},
            "as_of": {"type": "integer"},
            "source": {"type": "string"},
            "age_ms": {"type": "integer"},
            "trust": {"type": "string"},
            "schema_version": {"type": "string"},
            "open_orders_count": {"type": "integer"},
            "risk_units_free": {"type": ["number", "null"]},
            "risk_units_total": {"type": ["number", "null"]},
            "risk_units_used": {"type": ["number", "null"]},
            "budget_pct_used": {"type": ["number", "null"]},
            "concentration_breach": {"type": "boolean"},
            "symbols": {"type": "array"},
        },
    },
}


class AnalysisToolBroker:
    """Read-only evidence broker (E4/E5). Never writes, never touches network.

    ``repo`` is any object exposing the five read seams used below:
    ``get_candles``, ``latest_skill_result_refs``, ``latest_analysis_states``,
    ``list_active_opportunity_watches_for_symbol``, ``list_open_paper_orders``.
    """

    METHODS = frozenset({
        "latest_closed_market_summary",
        "deterministic_skill_evidence",
        "previous_round_state",
        "relevant_watch_evidence",
        "simulated_account_state",
    })

    def __init__(
        self,
        repo: Any,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_result_bytes: int = MAX_RESULT_BYTES,
        max_string_len: int = MAX_STRING_LEN,
        max_list_len: int = MAX_LIST_LEN,
        now_ms: int | None = None,
    ) -> None:
        self._repo = repo
        self._timeout_ms = max(1, int(float(timeout_s) * 1000))
        self.max_result_bytes = max(1, int(max_result_bytes))
        self.max_string_len = max(1, int(max_string_len))
        self.max_list_len = max(1, int(max_list_len))
        self._now = (lambda: int(now_ms)) if now_ms is not None else utc_ms

    # ── param validation (E5) ──────────────────────────────────────────────
    def _validate_symbol(self, symbol: Any) -> str:
        if not isinstance(symbol, str) or not _SYMBOL_RE.match(symbol):
            raise BrokerParamError(f"invalid symbol {symbol!r}; expected [A-Z0-9]{{2,20}}")
        return symbol.upper()

    def _validate_timeframe(self, timeframe: Any) -> None:
        if timeframe not in ALLOWED_TIMEFRAMES:
            raise BrokerParamError(
                f"invalid timeframe {timeframe!r}; allowed {ALLOWED_TIMEFRAMES}"
            )

    def _validate_regime(self, regime: Any) -> None:
        if regime not in ALLOWED_REGIMES:
            raise BrokerParamError(f"invalid regime {regime!r}; allowed {ALLOWED_REGIMES}")

    def _validate_side(self, side: Any) -> str:
        if not isinstance(side, str) or side not in _ALLOWED_SIDES:
            raise BrokerParamError(f"invalid side {side!r}; expected {_ALLOWED_SIDES}")
        return side

    def _at(self, analysis_time_utc: int | None) -> int:
        return int(analysis_time_utc) if analysis_time_utc is not None else self._now()

    # ── E4 read-only enforcement ───────────────────────────────────────────
    def __getattr__(self, name: str) -> Any:
        if name in FORBIDDEN_OPERATIONS:
            raise BrokerForbiddenError(
                f"AnalysisToolBroker is read-only; '{name}' is forbidden"
            )
        raise AttributeError(f"{type(self).__name__} has no attribute {name!r}")

    def call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        """Central read-only dispatch (E4). Returns an ``{"ok": True, "data"}``
        envelope on success, ``{"ok": False, "error": "read_failed"}`` when the
        underlying read source fails, and raises the broker's own error types on
        forbidden/param/timeout/size/schema violations."""
        # 08-10 Step 6: the two narrow risk reads dispatch here too, but they
        # are NOT added to ``METHODS`` (frozen by the 08-04 contract test).
        if method not in self.METHODS and method not in RISK_READ_METHODS:
            raise BrokerForbiddenError(
                f"AnalysisToolBroker exposes only read-only methods; rejected {method!r}"
            )
        fn = getattr(self, method)
        start_ms = _monotonic_ms()
        try:
            data = fn(**kwargs)
        except (BrokerParamError, BrokerForbiddenError, BrokerTimeoutError,
                BrokerSizeBudgetError, BrokerSchemaError, BrokerStaleError):
            raise
        except Exception as exc:  # noqa: BLE001 — read source failure is not a broker bug
            _LOGGER.warning("analysis broker %s read_failed: %s", method, exc)
            return {
                "ok": False,
                "method": method,
                "error": "read_failed",
                "message": str(exc)[:200],
                "elapsed_ms": int(_monotonic_ms() - start_ms),
            }
        elapsed_ms = int(_monotonic_ms() - start_ms)
        if elapsed_ms > self._timeout_ms:
            raise BrokerTimeoutError(
                f"{method} exceeded per-call timeout {self._timeout_ms}ms (took {elapsed_ms}ms)"
            )
        # E5 size budget: trim then fail-close if still too large.
        data = self._apply_size_budget(data)
        schema = RESULT_SCHEMAS.get(method)
        if schema:
            errors = _validate_against_schema(data, schema)
            if errors:
                raise BrokerSchemaError(
                    f"{method} result schema violation: {'; '.join(errors)}"
                )
        return {"ok": True, "method": method, "elapsed_ms": elapsed_ms, "data": data}

    def _apply_size_budget(self, data: Any) -> Any:
        trimmed = _trim_value(data, self.max_string_len, self.max_list_len)
        try:
            size = len(json.dumps(trimmed, ensure_ascii=False))
        except (TypeError, ValueError):
            return trimmed
        if size > self.max_result_bytes:
            raise BrokerSizeBudgetError(
                f"result exceeds size budget of {self.max_result_bytes} bytes (got {size})"
            )
        return trimmed

    # ── the five read-only methods (E4) ────────────────────────────────────
    def latest_closed_market_summary(
        self,
        symbol: str,
        timeframe: str,
        analysis_time_utc: int | None = None,
    ) -> dict[str, Any]:
        symbol = self._validate_symbol(symbol)
        self._validate_timeframe(timeframe)
        at = self._at(analysis_time_utc)
        candles = list(self._repo.get_candles(symbol, timeframe, analysis_time_utc=at, limit=200) or [])
        data: dict[str, Any] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "analysis_time_utc": at,
            "count": len(candles),
            "latest_close_time": None,
            "last_close": None,
            "last_ohlc": None,
        }
        if candles:
            last = candles[-1]
            data["latest_close_time"] = int(last.get("close_time") or at)
            try:
                data["last_close"] = float(last.get("close"))
            except (TypeError, ValueError):
                data["last_close"] = None
            data["last_ohlc"] = {
                k: last.get(k) for k in ("open", "high", "low", "close")
            }
        return data

    def deterministic_skill_evidence(
        self,
        symbol: str,
        timeframe: str,
        analysis_time_utc: int | None = None,
    ) -> dict[str, Any]:
        symbol = self._validate_symbol(symbol)
        self._validate_timeframe(timeframe)
        at = self._at(analysis_time_utc)
        refs = self._repo.latest_skill_result_refs(symbol, at) or {}
        clean = {}
        for k, v in refs.items():
            try:
                clean[str(k)] = int(v)
            except (TypeError, ValueError):
                clean[str(k)] = 0
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "analysis_time_utc": at,
            "skill_refs": clean,
            "count": len(clean),
        }

    def previous_round_state(self, symbol: str | None = None) -> dict[str, Any]:
        if symbol is not None:
            symbol = self._validate_symbol(symbol)
            row = self._repo.latest_analysis_state(symbol)
        else:
            rows = list(self._repo.latest_analysis_states(limit=1) or [])
            row = rows[0] if rows else None
        if not row:
            return {"latest": None}
        state = row.get("state") or {}
        return {
            "latest": {
                "symbol": row.get("symbol"),
                "analysis_time_utc": int(row.get("analysis_time") or 0),
                "analysis_state_id": row.get("id"),
                "decision": state.get("decision"),
                "signal_grade": state.get("signal_grade"),
                "confidence": state.get("confidence"),
            }
        }

    def relevant_watch_evidence(self, symbol: str, regime: str = "normal") -> dict[str, Any]:
        symbol = self._validate_symbol(symbol)
        self._validate_regime(regime)
        watches = list(self._repo.list_active_opportunity_watches_for_symbol(symbol) or [])
        out: list[dict[str, Any]] = []
        for w in watches:
            if regime == "extreme" and str(w.get("direction") or "").upper() == "LONG":
                continue
            out.append({
                "id": int(w.get("id") or 0),
                "symbol": symbol,
                "direction": w.get("direction"),
                "status": w.get("status"),
                # watch_reason is untrusted free text — length-bound for the LLM
                "watch_reason": str(w.get("watch_reason") or "")[:200],
                "untrusted_data": True,
            })
        return {"symbol": symbol, "regime": regime, "watches": out, "count": len(out)}

    def simulated_account_state(self) -> dict[str, Any]:
        orders = list(self._repo.list_open_paper_orders() or [])
        symbols = sorted({str(o.get("symbol") or "") for o in orders if o.get("symbol")})
        risk_values: list[float] = []
        for o in orders:
            r = o.get("risk_units")
            try:
                if r is not None:
                    risk_values.append(float(r))
            except (TypeError, ValueError):
                pass
        # Deterministic concentration heuristic: one live position per symbol is
        # allowed; exceeding 5 concurrent open orders is a breach.
        concentration_breach = len(orders) > 5
        return {
            "open_orders_count": len(orders),
            "symbols": symbols,
            "total_risk_units": round(sum(risk_values), 4) if risk_values else None,
            "concentration_breach": concentration_breach,
            "risk_override_required": False,
        }

    # ── 08-10 Step 6 narrow risk reads (design §6.2/§8) ───────────────────
    def confirmation_lifecycle_evidence(
        self,
        symbol: str,
        side: str,
        analysis_time_utc: int | None = None,
        max_age_ms: int | None = None,
    ) -> dict[str, Any]:
        """Read ONE prior trusted entry-confirmation lifecycle event for
        (symbol, side). Fail-closed on stale evidence: an event older than
        ``max_age_ms`` (default ``DEFAULT_LIFECYCLE_MAX_AGE_MS``) raises
        ``BrokerStaleError``. A repo row of ``None`` yields ``status: "absent"``
        (not stale — absence is a downstream determinism decision)."""
        symbol = self._validate_symbol(symbol)
        side = self._validate_side(side)
        at = self._at(analysis_time_utc)
        row = self._repo.confirmation_lifecycle(symbol, side, analysis_time_utc=at)
        if not row:
            return {
                "symbol": symbol,
                "side": side,
                "analysis_time_utc": at,
                "status": "absent",
                "fingerprint": None,
                "direction": None,
                "close_time": None,
                "age_bars": None,
                "source": "analysis_tool_broker",
                "as_of": at,
                "age_ms": 0,
                "trust": "trusted",
                "schema_version": "confirmation_lifecycle_v1",
            }
        event_time = int(row.get("close_time") or row.get("as_of") or at)
        age_ms = max(0, at - event_time)
        cap = int(max_age_ms) if max_age_ms is not None else DEFAULT_LIFECYCLE_MAX_AGE_MS
        if age_ms > cap:
            raise BrokerStaleError(
                f"confirmation lifecycle evidence stale: age_ms={age_ms} > "
                f"max_age_ms={cap} (as_of={event_time}, at={at})"
            )
        return {
            "symbol": symbol,
            "side": side,
            "analysis_time_utc": at,
            "status": str(row.get("status") or "confirmed"),
            "fingerprint": row.get("fingerprint"),
            "direction": row.get("direction"),
            "close_time": event_time,
            "age_bars": row.get("age_bars"),
            "source": "analysis_tool_broker",
            "as_of": int(row.get("as_of") or event_time),
            "age_ms": age_ms,
            "trust": "trusted",
            "schema_version": "confirmation_lifecycle_v1",
        }

    def adaptive_risk_budget(
        self,
        symbol: str | None = None,
        analysis_time_utc: int | None = None,
    ) -> dict[str, Any]:
        """Compact account risk budget summary (per-symbol when ``symbol`` is
        given, whole account otherwise). Reads only the ``adaptive_risk_budget_
        summary`` repo seam — never raw account rows."""
        sym = self._validate_symbol(symbol) if symbol is not None else None
        at = self._at(analysis_time_utc)
        row = self._repo.adaptive_risk_budget_summary(sym, as_of=at) or {}
        return {
            "symbol": sym,
            "as_of": at,
            "source": "analysis_tool_broker",
            "age_ms": 0,
            "trust": "trusted",
            "schema_version": "adaptive_risk_budget_v1",
            "open_orders_count": int(row.get("open_orders_count") or 0),
            "risk_units_free": row.get("risk_units_free"),
            "risk_units_total": row.get("risk_units_total"),
            "risk_units_used": row.get("risk_units_used"),
            "budget_pct_used": row.get("budget_pct_used"),
            "concentration_breach": bool(row.get("concentration_breach") or False),
            "symbols": [str(s) for s in (row.get("symbols") or [])],
        }


class BrokerRoundManager:
    """E6: caps tool requests per round (MAX_TOOL_REQUESTS_PER_ROUND) and
    total rounds (MAX_ROUNDS). Each request is dispatched through the broker and
    recorded in ``requests`` with its per-round index."""

    def __init__(self, broker: AnalysisToolBroker) -> None:
        self.broker = broker
        self.rounds: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.requests_used = 0
        self._current: dict[str, Any] | None = None
        self._current_count = 0

    def begin_round(self, label: str) -> None:
        if len(self.rounds) >= MAX_ROUNDS:
            raise BrokerRoundLimitError(f"exceeded max rounds {MAX_ROUNDS}")
        self._current = {"round": label, "request_indexes": []}
        self._current_count = 0
        self.rounds.append(self._current)

    def request(self, method: str, **kwargs: Any) -> dict[str, Any]:
        if self._current is None:
            raise BrokerRoundLimitError("begin_round must be called before request")
        if self._current_count >= MAX_TOOL_REQUESTS_PER_ROUND:
            raise BrokerRoundLimitError(
                f"round '{self._current['round']}' exceeds MAX_TOOL_REQUESTS_PER_ROUND={MAX_TOOL_REQUESTS_PER_ROUND}"
            )
        env = self.broker.call(method, **kwargs)
        self._current["request_indexes"].append(len(self.requests))
        self.requests.append({"method": method, "kwargs": kwargs, "env": env})
        self.requests_used += 1
        self._current_count += 1
        return env

    def end_round(self) -> None:
        self._current = None
        self._current_count = 0


def _run_verifier(
    manager: BrokerRoundManager,
    *,
    symbol: str,
    timeframe: str,
    analysis_time_utc: int,
    deterministic_risk_ok: bool,
) -> dict[str, Any]:
    """E8: a VETO-ONLY verifier round. It can only confirm or veto an order
    candidate; it can never grant order eligibility on its own."""
    reasons: list[str] = []
    if not deterministic_risk_ok:
        reasons.append("deterministic_risk_gate_blocked")

    market = manager.request(
        "latest_closed_market_summary", symbol=symbol, timeframe=timeframe,
        analysis_time_utc=analysis_time_utc,
    )
    if not market.get("ok"):
        reasons.append("evidence_unavailable:market_summary")
    skill = manager.request(
        "deterministic_skill_evidence", symbol=symbol, timeframe=timeframe,
        analysis_time_utc=analysis_time_utc,
    )
    if not skill.get("ok"):
        reasons.append("evidence_unavailable:skill_evidence")
    account = manager.request("simulated_account_state")
    if not account.get("ok"):
        reasons.append("evidence_unavailable:account_state")
    elif account.get("data", {}).get("concentration_breach"):
        reasons.append("concentration_breach")

    verdict = "veto" if reasons else "approve"
    return {
        "round": "verifier",
        "verdict": verdict,
        "reasons": reasons,
        # The verifier never emits a write instruction; it only returns a
        # boolean verdict that downstream deterministic code may act on.
        "bypass_attempted": False,
        "request_indexes": manager.rounds[-1]["request_indexes"] if manager.rounds else [],
    }


def run_analysis_rounds(
    broker: AnalysisToolBroker,
    *,
    symbol: str,
    timeframe: str,
    analysis_time_utc: int,
    conflict: bool = False,
    watch_hit: bool = False,
    order_candidate: bool = False,
    deterministic_risk_ok: bool = False,
) -> dict[str, Any]:
    """E7/E8 orchestration.

    - ``normal`` single round for an ordinary quote (2 requests).
    - ``supplement`` evidence round when a conflict or a watch hit is present.
    - ``verifier`` VETO-ONLY round when an order candidate is present.
    ``order_allowed`` is True only when the candidate is present, the verifier
    approves, AND the deterministic risk gate is open — so the verifier can
    never bypass the risk gate (E8).
    """
    manager = BrokerRoundManager(broker)

    manager.begin_round("normal")
    manager.request("latest_closed_market_summary", symbol=symbol, timeframe=timeframe, analysis_time_utc=analysis_time_utc)
    manager.request("deterministic_skill_evidence", symbol=symbol, timeframe=timeframe, analysis_time_utc=analysis_time_utc)
    manager.end_round()

    if conflict or watch_hit:
        # Evidence supplement round: reconcile against the previous round state
        # AND pull active watch evidence, so the follow-up decision sees both
        # continuity and watch context (E7).
        manager.begin_round("supplement")
        manager.request("previous_round_state", symbol=symbol)
        manager.request("relevant_watch_evidence", symbol=symbol, regime="normal")
        manager.end_round()

    verifier: dict[str, Any] | None = None
    if order_candidate:
        manager.begin_round("verifier")
        verifier = _run_verifier(
            manager,
            symbol=symbol,
            timeframe=timeframe,
            analysis_time_utc=analysis_time_utc,
            deterministic_risk_ok=deterministic_risk_ok,
        )
        manager.end_round()

    # E8: order_allowed requires the candidate, an approving verifier AND an
    # open deterministic risk gate. A blocked gate always vetoes.
    order_allowed = bool(
        order_candidate
        and verifier is not None
        and verifier["verdict"] == "approve"
        and deterministic_risk_ok
    )

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "analysis_time_utc": analysis_time_utc,
        "rounds": manager.rounds,
        "requests": manager.requests,
        "requests_used": manager.requests_used,
        "verifier": verifier,
        "order_allowed": order_allowed,
    }


# ── 08-10 Step 6: structured tool-request schema + supplement executor ─────
# Per-method param spec used by ``validate_tool_request``. Every risk read
# declares its own parameter surface; unknown param keys are rejected so a
# hostile/forged request can never smuggle extra arguments into a read.
RISK_READ_PARAM_SPECS: dict[str, dict[str, dict[str, Any]]] = {
    "confirmation_lifecycle_evidence": {
        "symbol": {"type": "string", "required": True},
        "side": {"type": "string", "required": True, "enum": _ALLOWED_SIDES},
        "analysis_time_utc": {"type": "integer", "required": False},
        "max_age_ms": {"type": "integer", "required": False},
    },
    "adaptive_risk_budget": {
        "symbol": {"type": "string", "required": False},
        "analysis_time_utc": {"type": "integer", "required": False},
    },
    "latest_closed_market_summary": {
        "symbol": {"type": "string", "required": True},
        "timeframe": {"type": "string", "required": True, "enum": ALLOWED_TIMEFRAMES},
        "analysis_time_utc": {"type": "integer", "required": False},
    },
    "deterministic_skill_evidence": {
        "symbol": {"type": "string", "required": True},
        "timeframe": {"type": "string", "required": True, "enum": ALLOWED_TIMEFRAMES},
        "analysis_time_utc": {"type": "integer", "required": False},
    },
    "relevant_watch_evidence": {
        "symbol": {"type": "string", "required": True},
        "regime": {"type": "string", "required": False, "enum": ALLOWED_REGIMES},
    },
    "simulated_account_state": {},
}


def validate_tool_request(
    request: Any,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Validate one structured tool request BEFORE it reaches the broker.

    Returns ``(ok, err, normalized)`` where ``normalized`` is a clean copy
    ``{"method": ..., "params": ...}``. Rejects non-object requests, any key
    other than ``method``/``params``, unknown/non-enumerated methods,
    non-object params, unknown param keys, wrong param types and bad enum
    values (side/timeframe/regime). Fail-closed: nothing here executes anything.
    """
    if not isinstance(request, dict):
        return False, "tool request must be an object", None
    keys = set(request)
    if keys != {"method", "params"}:
        extra = sorted(keys - {"method", "params"})
        missing = [k for k in ("method", "params") if k not in request]
        return False, (
            f"tool request must carry exactly 'method' and 'params' "
            f"(extra={extra}, missing={missing})"
        ), None
    method = request.get("method")
    if not isinstance(method, str) or method not in RISK_READ_METHODS:
        return False, f"unknown or non-enumerated tool method: {method!r}", None
    params = request.get("params")
    if not isinstance(params, dict):
        return False, "tool request params must be an object", None
    spec = RISK_READ_PARAM_SPECS.get(method, {})
    unknown = sorted(set(params) - set(spec))
    if unknown:
        return False, f"unknown param(s) for {method}: {unknown}", None
    for name, rule in spec.items():
        if rule.get("required") and name not in params:
            return False, f"missing required param {method}.{name}", None
        if name in params:
            value = params[name]
            expected = rule["type"]
            if not _TYPECHECK.get(expected, lambda _: True)(value):
                return False, (
                    f"param {method}.{name} must be {expected}, "
                    f"got {type(value).__name__}"
                ), None
            enum = rule.get("enum")
            if enum is not None and value not in enum:
                return False, f"param {method}.{name} must be one of {enum}, got {value!r}", None
    return True, None, {"method": method, "params": dict(params)}


def _risk_evidence_meta(method: str, env: dict[str, Any], at: int) -> dict[str, Any]:
    """Standard source/as-of/age/trust/schema metadata for one executed result.

    ``as_of`` prefers the result's own self-declared ``as_of`` (when the
    underlying fact was true), then its ``analysis_time_utc``, then the round
    time; ``age_ms`` is measured against the round analysis time.
    """
    data = env.get("data")
    as_of: Any = None
    if isinstance(data, dict):
        if isinstance(data.get("as_of"), int):
            as_of = data["as_of"]
        elif isinstance(data.get("analysis_time_utc"), int):
            as_of = data["analysis_time_utc"]
    if not isinstance(as_of, int):
        as_of = at
    return {
        "source": "analysis_tool_broker",
        "as_of": as_of,
        "age_ms": max(0, int(at) - int(as_of)),
        "trust": RISK_READ_TRUST.get(method, "untrusted_data"),
        "schema_version": RISK_READ_SCHEMA_VERSION.get(method, "unknown"),
    }


def run_risk_supplement_round(
    broker: AnalysisToolBroker,
    *,
    requests: list[Any],
    symbol: str,
    analysis_time_utc: int,
    max_requests: int = MAX_RISK_TOOL_REQUESTS,
) -> dict[str, Any]:
    """Execute one validated risk supplement request list (design §6.2 #2/#3).

    - Over-capacity (more than ``max_requests``) raises ``BrokerRoundLimitError``;
    - an invalid/unknown request raises ``BrokerForbiddenError`` — the round
      stops there ("more requests, unknown methods ... returns ``wait``");
    - any executed evidence that fails (``read_failed``) or raises a broker
      error (stale/param/size/schema/timeout) is recorded as ``ok=False`` with
      ``error="evidence_failed"`` so the round is never approvable;
    - every successful result is stamped with source/as-of/age/trust/schema
      metadata (``env["meta"]``).

    Returns ``{"ok", "symbol", "analysis_time_utc", "results", "requests_used"}``.
    """
    if not isinstance(requests, list) or len(requests) > max_requests:
        raise BrokerRoundLimitError(
            f"risk supplement round exceeds max_requests={max_requests} "
            f"(got {len(requests) if isinstance(requests, list) else 'non-list'})"
        )
    at = int(analysis_time_utc)
    results: list[dict[str, Any]] = []
    for i, req in enumerate(requests):
        ok, err, normalized = validate_tool_request(req)
        if not ok:
            raise BrokerForbiddenError(f"tool request {i} rejected: {err}")
        method = normalized["method"]
        params = normalized["params"]
        # Pin reads that accept an as-of to the round time when the request
        # omitted it, so evidence age is measured against this proposal round.
        if "analysis_time_utc" in RISK_READ_PARAM_SPECS.get(method, {}) and "analysis_time_utc" not in params:
            params["analysis_time_utc"] = at
        try:
            env = broker.call(method, **params)
        except BrokerRoundLimitError:
            raise
        except Exception as exc:  # noqa: BLE001 — fail closed on any broker error
            results.append({
                "ok": False,
                "method": method,
                "error": "evidence_failed",
                "message": str(exc)[:200],
                "fail_closed_reason": type(exc).__name__,
                "elapsed_ms": 0,
            })
            continue
        if not env.get("ok"):
            results.append({**env, "ok": False, "error": "evidence_failed"})
            continue
        env["meta"] = _risk_evidence_meta(method, env, at)
        results.append(env)
    return {
        "ok": all(r.get("ok") for r in results),
        "symbol": symbol,
        "analysis_time_utc": at,
        "results": results,
        "requests_used": len(results),
    }
