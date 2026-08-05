# -*- coding: utf-8 -*-
"""08-04 contract C: versioned LLM context envelope (C1/C2/C3/C4/C9/C10).

``build_context_envelope`` packages deterministic market facts, derived skill
evidence, bounded memory and execution state into a single versioned envelope
where every item carries source / symbol / timeframe / as_of / age_ms /
provenance / trust_level (and ``evidence_id`` for derived evidence). It is a
PURE builder: callers fetch the lists (watches, orders, memory) and pass them
in, so the contract is testable without a DB.

Trust rules enforced here:
- ``trusted_facts``       -> deterministic snapshot facts, trust_level=trusted.
- ``derived_evidence``    -> schema-validated deterministic skill results,
                             trust_level=model_derived, each with evidence_id.
                             prompt.md / skill_contract free text is stripped.
- ``bounded_memory``      -> skill_feedback_memory, trust_level=untrusted_data,
                             bounded length, can never grant confidence / grade
                             / order eligibility (C6/C9).
- ``execution_state``     -> open paper orders (<=5) + active watches (<=3,
                             same symbol, unexpired, not superseded) (C3).
- ``counter_evidence``    -> triggered / invalidated / expired / superseded
                             watches, trust_level=counter_evidence, cannot raise
                             grade/confidence (C4).

Malicious free text (C10) therefore only ever appears inside untrusted_data /
counter_evidence display fields — never as a trusted instruction and never as
an order-eligibility signal.
"""
from __future__ import annotations

from typing import Any

ENVELOPE_VERSION = "1.0"

TRUST_LEVELS = ("trusted", "model_derived", "counter_evidence", "untrusted_data")

# Free-text / instruction-bearing keys that must never be emitted as high-trust
# content by this builder (C8). The LLM-facing compact snapshot strips these too.
_STRIP_KEYS = ("prompt", "prompt_md", "skill_yaml_text", "skill_contract", "ga_interpretation")


def _age_ms(now_ms: int, as_of: int) -> int:
    return max(0, int(now_ms) - int(as_of))


def _fact(source: str, symbol: str, timeframe: str, as_of: int, now_ms: int, provenance: str, value: Any) -> dict[str, Any]:
    return {
        "source": source,
        "symbol": symbol,
        "timeframe": timeframe,
        "as_of": as_of,
        "age_ms": _age_ms(now_ms, as_of),
        "provenance": provenance,
        "trust_level": "trusted",
        "value": value,
    }


def _strip_skill_free_text(item: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a skill module with instruction/contract free text removed."""
    out = {}
    for k, v in item.items():
        if k in _STRIP_KEYS:
            continue
        if isinstance(v, dict):
            out[k] = _strip_skill_free_text(v)
        elif isinstance(v, list):
            out[k] = [
                _strip_skill_free_text(x) if isinstance(x, dict) else x
                for x in v
            ]
        else:
            out[k] = v
    return out


def _is_expired(watch: dict[str, Any], now_ms: int) -> bool:
    expires = watch.get("expires_at")
    if expires is None or expires == "":
        return False
    try:
        return int(expires) <= now_ms
    except (TypeError, ValueError):
        # A non-numeric / ISO expiry is treated as still-valid (fail-open on
        # parse only; the status + supersession filters are the hard gates).
        return False


def _watch_regime_relevant(watch: dict[str, Any], regime: str) -> bool:
    """A watch is regime-relevant unless the regime is extreme and the watch
    direction opposes a hard risk stance (extreme regime -> no new longs)."""
    if regime != "extreme":
        return True
    return str(watch.get("direction") or "").upper() != "LONG"


def build_context_envelope(
    *,
    repo: Any,
    symbol: str,
    timeframe: str,
    analysis_time_utc: int,
    snapshot: dict[str, Any] | None,
    previous_state: dict[str, Any] | None,
    watches: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    memory: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the versioned context envelope.

    ``repo`` is accepted for caller symmetry (future reads); this builder is
    deterministic on its inputs and does not query the DB.
    """
    now_ms = int(analysis_time_utc or 0)
    snapshot = snapshot or {}
    as_of = int(snapshot.get("analysis_time_utc") or now_ms) if snapshot.get("analysis_time_utc") is not None else now_ms
    regime_cfg = (snapshot.get("modules") or {}).get("market_regime") or {}
    regime = regime_cfg.get("regime") or "normal"
    newer_decision_id = None
    if previous_state:
        newer_decision_id = previous_state.get("decision_id") or previous_state.get("id")

    # ── trusted_facts: deterministic snapshot facts ──────────────────────
    trusted_facts: list[dict[str, Any]] = []
    if snapshot.get("symbol"):
        trusted_facts.append(_fact("snapshot.symbol", symbol, timeframe, as_of, now_ms, "market_state_snapshot", snapshot["symbol"]))
    if snapshot.get("mode"):
        trusted_facts.append(_fact("snapshot.mode", symbol, timeframe, as_of, now_ms, "market_state_snapshot", snapshot["mode"]))
    dq = snapshot.get("data_quality") or {}
    if isinstance(dq, dict):
        trusted_facts.append(_fact(
            "snapshot.data_quality", symbol, timeframe, as_of, now_ms, "market_state_snapshot",
            {"status": dq.get("status"), "closed_candles_only": dq.get("closed_candles_only")},
        ))
    if regime_cfg.get("regime"):
        trusted_facts.append(_fact(
            "snapshot.modules.market_regime", symbol, timeframe, as_of, now_ms, "market_state_snapshot",
            {"regime": regime, "extreme": bool(regime_cfg.get("extreme"))},
        ))

    # ── derived_evidence: schema-validated deterministic skill results ───
    derived_evidence: list[dict[str, Any]] = []
    modules = snapshot.get("modules") or {}
    for skill_name in ("price_action", "momentum", "trend_stage", "smc", "order_flow", "chanlun"):
        mod = modules.get(skill_name)
        if not isinstance(mod, dict):
            continue
        clean = _strip_skill_free_text(mod)
        derived_evidence.append({
            "source": f"skill:{skill_name}",
            "symbol": symbol,
            "timeframe": timeframe,
            "as_of": as_of,
            "age_ms": _age_ms(now_ms, as_of),
            "provenance": skill_name,
            "trust_level": "model_derived",
            "evidence_id": f"{skill_name}:{symbol}:{timeframe}:{as_of}",
            "summary": clean,
        })

    # ── bounded_memory: untrusted_data, bounded, cannot grant eligibility ─
    bounded_memory: list[dict[str, Any]] = []
    for mem in (memory or [])[:8]:
        item = dict(mem)
        item["source"] = str(mem.get("source_type") or "skill_feedback_memory")
        item["symbol"] = symbol
        item["timeframe"] = timeframe
        item["as_of"] = int(mem["as_of"]) if mem.get("as_of") is not None else as_of
        item["age_ms"] = _age_ms(now_ms, item["as_of"])
        item["provenance"] = str(mem.get("skill_name") or "skill_feedback_memory")
        item["trust_level"] = "untrusted_data"
        item["untrusted_data"] = True
        # Findings/adjustments are free text: bound their length, keep as display.
        for key in ("finding", "pattern_description"):
            if isinstance(item.get(key), str) and len(item[key]) > 200:
                item[key] = item[key][:200] + "…"
        bounded_memory.append(item)

    # ── watches: active (<=3) vs counter_evidence (C3/C4) ────────────────
    active_watches: list[dict[str, Any]] = []
    counter_evidence: list[dict[str, Any]] = []
    for watch in watches or []:
        if str(watch.get("symbol") or "") != symbol:
            continue
        status = str(watch.get("status") or "active")
        expired = _is_expired(watch, now_ms)
        superseded = (
            newer_decision_id is not None
            and watch.get("ga_decision_id") is not None
            and int(watch["ga_decision_id"]) < int(newer_decision_id)
        )
        if status != "active" or expired or superseded:
            item = {
                "id": watch.get("id"),
                "symbol": symbol,
                "direction": watch.get("direction"),
                "status": status,
                "watch_reason": str(watch.get("watch_reason") or "")[:200],
                "source": "opportunity_watch",
                "timeframe": timeframe,
                "as_of": as_of,
                "age_ms": _age_ms(now_ms, as_of),
                "provenance": "opportunity_watcher",
                "trust_level": "counter_evidence",
                "untrusted_data": True,
            }
            if superseded:
                item["superseded"] = True
            counter_evidence.append(item)
            continue
        if not _watch_regime_relevant(watch, regime):
            counter_evidence.append({
                "id": watch.get("id"), "symbol": symbol, "direction": watch.get("direction"),
                "status": status,
                "watch_reason": str(watch.get("watch_reason") or "")[:200],
                "source": "opportunity_watch", "timeframe": timeframe, "as_of": as_of,
                "age_ms": _age_ms(now_ms, as_of), "provenance": "opportunity_watcher",
                "trust_level": "counter_evidence", "untrusted_data": True,
                "regime_relevant": False,
            })
            continue
        if len(active_watches) >= 3:
            break
        active_watches.append({
            "id": watch.get("id"),
            "symbol": symbol,
            "direction": watch.get("direction"),
            "status": status,
            "watch_reason": str(watch.get("watch_reason") or "")[:200],
            "source": "opportunity_watch",
            "timeframe": timeframe,
            "as_of": as_of,
            "age_ms": _age_ms(now_ms, as_of),
            "provenance": "opportunity_watcher",
            "trust_level": "untrusted_data",
            "untrusted_data": True,
            "regime_relevant": True,
            "expires_at": watch.get("expires_at"),
        })

    # ── execution_state: bounded open orders ─────────────────────────────
    open_orders: list[dict[str, Any]] = []
    for order in (orders or [])[:5]:
        open_orders.append({
            "id": order.get("id"),
            "symbol": str(order.get("symbol") or symbol),
            "side": order.get("side"),
            "entry_price": order.get("entry_price"),
            "status": order.get("status"),
            "source": "paper_orders",
            "timeframe": timeframe,
            "as_of": as_of,
            "age_ms": _age_ms(now_ms, as_of),
            "provenance": "paper_broker",
            "trust_level": "trusted",
        })

    return {
        "envelope_version": ENVELOPE_VERSION,
        "symbol": symbol,
        "timeframe": timeframe,
        "analysis_time_utc": now_ms,
        "trusted_facts": trusted_facts,
        "derived_evidence": derived_evidence,
        "bounded_memory": bounded_memory,
        "execution_state": {
            "active_watches": active_watches,
            "open_orders": open_orders,
        },
        "counter_evidence": counter_evidence,
    }
