from __future__ import annotations

from typing import Any

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.storage.repository import CryptoGuardRepository

EXTREME_REGIMES = {"extreme_volatility", "funding_shock", "news_like_event", "low_liquidity"}

# BTC/ETH as market leaders
LEADER_SYMBOLS = ("BTCUSDT", "ETHUSDT")

# Timeframes for regime analysis
REGIME_TIMEFRAMES = ("15m", "1h", "4h")


def score_market_regime(
    repo: CryptoGuardRepository,
    *,
    symbol: str,
    analysis_time_utc: int,
    decision_side: str = "",
) -> dict[str, Any]:
    """Full market regime scoring for GA decision context.

    Analyzes BTC/ETH bias, market phase, breadth, volatility, symbol relative strength,
    and produces regime_alignment + adjustments for risk gating.

    Returns dict compatible with market_regime_json storage.
    """
    reasons: list[str] = []

    # 1. Load BTC/ETH candles (need >=21 for EMA21 in _bias_from_candles)
    btc_4h = _load_candles(repo, "BTCUSDT", "4h", analysis_time_utc, limit=30)
    btc_1h = _load_candles(repo, "BTCUSDT", "1h", analysis_time_utc, limit=30)
    eth_4h = _load_candles(repo, "ETHUSDT", "4h", analysis_time_utc, limit=30)
    eth_1h = _load_candles(repo, "ETHUSDT", "1h", analysis_time_utc, limit=30)

    # 2. BTC/ETH bias from 4h structure
    btc_bias = _bias_from_candles(btc_4h, "BTCUSDT")
    eth_bias = _bias_from_candles(eth_4h, "ETHUSDT")

    # 3. Market phase
    market_phase = _market_phase(btc_1h, btc_4h, eth_1h)
    reasons.append(f"market_phase={market_phase} btc={btc_bias} eth={eth_bias}")

    # 4. Breadth: sample direction from active symbols
    active_symbols = repo.active_analysis_symbols()
    breadth_score = _breadth_score(repo, active_symbols, analysis_time_utc)
    if abs(breadth_score) > 0.3:
        reasons.append(f"breadth={breadth_score:+.2f}")

    # 5. Volatility state
    volatility_state = _volatility_state(btc_1h[-12:] if btc_1h else [])
    if volatility_state != "normal":
        reasons.append(f"volatility={volatility_state}")

    # 6. Symbol relative strength vs BTC
    symbol_rs = "neutral"
    if symbol not in LEADER_SYMBOLS:
        sym_1h = _load_candles(repo, symbol, "1h", analysis_time_utc, limit=30)
        sym_4h = _load_candles(repo, symbol, "4h", analysis_time_utc, limit=30)
        symbol_rs = _relative_strength(sym_4h, btc_4h, sym_1h, btc_1h)
        if symbol_rs != "neutral":
            reasons.append(f"relative_strength={symbol_rs}")

    # Load config for weights and independent_trend settings
    cfg = load_config().trading_mode
    regime_cfg = cfg.get("market_regime", {})

    # 7. Regime alignment vs decision side
    independent_trend_cfg = regime_cfg.get("independent_trend")
    regime_alignment, alignment_reason = _regime_alignment(
        market_phase, btc_bias, eth_bias, symbol_rs, decision_side, breadth_score,
        independent_trend_cfg=independent_trend_cfg,
    )
    reasons.append(alignment_reason)

    # 8. Adjustments
    confidence_adjustment = 0.0
    risk_multiplier = 1.0
    require_stronger_confirmation = False

    if regime_alignment == "counter_regime":
        if market_phase in {"rebound", "risk_on"} and decision_side == "SHORT":
            confidence_adjustment = -0.10
            risk_multiplier = 0.75
            require_stronger_confirmation = True
            reasons.append("counter_regime: 反弹/risk_on阶段做空，降低信心并提高确认要求")
        elif market_phase in {"selloff", "risk_off"} and decision_side == "LONG":
            confidence_adjustment = -0.10
            risk_multiplier = 0.75
            require_stronger_confirmation = True
            reasons.append("counter_regime: 抛售/risk_off阶段做多，降低信心并提高确认要求")
    elif regime_alignment == "aligned":
        confidence_adjustment = 0.05
        reasons.append("regime_aligned: 方向与大盘阶段一致")
    elif regime_alignment == "unclear":
        confidence_adjustment = 0.0
        require_stronger_confirmation = True
        reasons.append("数据不足，不调整信心但提高确认要求")

    # 8. Weighted regime score from config weights
    btc_weight = float(regime_cfg.get("btc_weight", 0.10))
    eth_weight = float(regime_cfg.get("eth_weight", 0.05))
    breadth_weight = float(regime_cfg.get("breadth_weight", 0.05))
    volatility_weight = float(regime_cfg.get("volatility_weight", 0.05))

    btc_score = 1 if btc_bias == "bullish" else (-1 if btc_bias == "bearish" else 0)
    eth_score = 1 if eth_bias == "bullish" else (-1 if eth_bias == "bearish" else 0)
    breadth_score_scaled = max(-1.0, min(1.0, breadth_score))
    if volatility_state == "spike":
        volatility_score = 1.0
    elif volatility_state == "elevated":
        volatility_score = 0.5
    else:
        volatility_score = 0.0

    weighted_regime_score = (
        btc_score * btc_weight
        + eth_score * eth_weight
        + breadth_score_scaled * breadth_weight
        + volatility_score * volatility_weight
    )

    return {
        "module": "market_regime",
        "symbol": symbol,
        "btc_bias": btc_bias,
        "eth_bias": eth_bias,
        "market_phase": market_phase,
        "breadth_score": round(breadth_score, 3),
        "volatility_state": volatility_state,
        "symbol_relative_strength": symbol_rs,
        "regime_alignment": regime_alignment,
        "confidence_adjustment": confidence_adjustment,
        "suggested_risk_multiplier": risk_multiplier,
        "require_stronger_confirmation": require_stronger_confirmation,
        "reasons": reasons,
        "analysis_time_utc": analysis_time_utc,
        "regime_score": round(weighted_regime_score, 4),
        "component_scores": {
            "btc_score": btc_score,
            "eth_score": eth_score,
            "breadth_score": round(breadth_score_scaled, 4),
            "volatility_score": volatility_score,
        },
        "component_weights": {
            "btc_weight": btc_weight,
            "eth_weight": eth_weight,
            "breadth_weight": breadth_weight,
            "volatility_weight": volatility_weight,
        },
    }


def classify_market_regime(candles: list[dict[str, Any]], *, analysis_time_utc: int) -> dict[str, Any]:
    """Classify extreme market regimes (volatility/liquidity shocks).

    This is the legacy single-symbol extreme detector. Use score_market_regime()
    for full multi-symbol regime scoring with BTC/ETH context.
    """
    if len(candles) < 30:
        return {
            "module": "market_regime",
            "regime": "normal",
            "extreme": False,
            "atr_percentile": 0.0,
            "volume_ratio": 0.0,
            "range_ratio": 0.0,
            "reasons": ["样本不足，按 normal 保守处理，不触发极端行情进化。"],
            "analysis_time_utc": analysis_time_utc,
        }

    ranges = [float(c["high"]) - float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    volumes = [float(c["volume"]) for c in candles]
    recent_range = ranges[-1]
    sorted_ranges = sorted(ranges[-60:] if len(ranges) >= 60 else ranges)
    atr_percentile = _percentile_rank(sorted_ranges, recent_range)
    close = closes[-1] or 1.0
    range_ratio = recent_range / close
    avg_volume = sum(volumes[-30:-1]) / max(1, len(volumes[-30:-1]))
    volume_ratio = volumes[-1] / avg_volume if avg_volume else 0.0
    wick_count = _recent_wick_count(candles[-5:])

    reasons: list[str] = []
    regime = "normal"
    if range_ratio >= 0.08 or (atr_percentile >= 0.95 and volume_ratio >= 2.5):
        regime = "news_like_event"
        reasons.append("单根波幅和成交量同时异常，按新闻/黑天鹅类事件处理。")
    elif atr_percentile >= 0.90 or range_ratio >= 0.045:
        regime = "extreme_volatility"
        reasons.append("ATR/单根波幅处于极端区间。")
    elif wick_count >= 3:
        regime = "low_liquidity"
        reasons.append("近期连续插针，可能存在流动性异常。")
    elif volume_ratio >= 2.2:
        regime = "high_volatility"
        reasons.append("成交量显著放大。")
    else:
        reasons.append("波动、成交量和插针行为未达到极端阈值。")

    return {
        "module": "market_regime",
        "regime": regime,
        "extreme": regime in EXTREME_REGIMES,
        "evolution_trigger_allowed": regime not in EXTREME_REGIMES,
        "atr_percentile": atr_percentile,
        "volume_ratio": volume_ratio,
        "range_ratio": range_ratio,
        "recent_wick_count": wick_count,
        "reasons": reasons,
        "analysis_time_utc": analysis_time_utc,
    }


# ── Internal helpers ────────────────────────────────────────────


def _load_candles(
    repo: CryptoGuardRepository,
    symbol: str,
    timeframe: str,
    analysis_time_utc: int,
    limit: int = 24,
) -> list[dict[str, Any]]:
    try:
        return repo.get_candles(symbol, timeframe, analysis_time_utc=analysis_time_utc, limit=limit)
    except Exception:
        return []


def _bias_from_candles(candles: list[dict[str, Any]], label: str) -> str:
    """Determine bullish/bearish/range/transition from 4h candles."""
    if len(candles) < 6:
        return "range"

    closes = [float(c["close"]) for c in candles]
    # Simple EMA crossover: EMA8 vs EMA21
    ema8 = _ema(closes, 8)
    ema21 = _ema(closes, 21)

    if ema8 is None or ema21 is None:
        return "range"

    # Trend strength: slope of EMA8 over last 4 candles
    if len(closes) >= 4:
        ema8_recent = _ema(closes[-4:], min(4, len(closes[-4:])))
        ema8_prev = _ema(closes[-8:-4], min(4, len(closes[-8:-4]))) if len(closes) >= 8 else ema8
    else:
        ema8_recent = ema8
        ema8_prev = ema8

    # Width between EMAs as % of price
    spread_pct = abs(ema8 - ema21) / ema21 * 100 if ema21 else 0

    if ema8 > ema21 * 1.005 and spread_pct > 0.3:
        return "bullish"
    elif ema8 < ema21 * 0.995 and spread_pct > 0.3:
        return "bearish"
    elif ema8_recent is not None and ema8_prev is not None and ema8_recent > ema8_prev * 1.002:
        return "transition"  # turning bullish
    elif ema8_recent is not None and ema8_prev is not None and ema8_recent < ema8_prev * 0.998:
        return "transition"  # turning bearish
    else:
        return "range"


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * multiplier + ema
    return ema


def _market_phase(
    btc_1h: list[dict[str, Any]],
    btc_4h: list[dict[str, Any]],
    eth_1h: list[dict[str, Any]],
) -> str:
    """Classify market phase: risk_on, risk_off, rebound, selloff, chop, unknown.

    Requires both BTC (1h+4h) and ETH (1h) data for joint confirmation.
    Returns "unknown" if BTC data is missing or ETH 1h has insufficient candles
    (< 21), triggering stronger_confirmation upstream.
    """
    if not btc_1h or not btc_4h:
        return "unknown"

    btc_bias_4h = _bias_from_candles(btc_4h, "BTC")
    # Use all available 1h candles for EMA21; bias still reflects short-term due to EMA responsiveness
    btc_bias_1h = _bias_from_candles(btc_1h, "BTC_1h") if len(btc_1h) >= 21 else btc_bias_4h

    # Determine phase from BTC
    phase = "chop"
    # Check for rebound: 1h turning bullish after 4h bearish/range
    if btc_bias_4h in {"bearish", "range", "transition"} and btc_bias_1h == "bullish":
        phase = "rebound"
    # Check for selloff: 1h turning bearish after 4h bullish/range
    elif btc_bias_4h in {"bullish", "range", "transition"} and btc_bias_1h == "bearish":
        phase = "selloff"
    # Strong trend alignment
    elif btc_bias_4h == "bullish" and btc_bias_1h in {"bullish", "transition"}:
        phase = "risk_on"
    elif btc_bias_4h == "bearish" and btc_bias_1h in {"bearish", "transition"}:
        phase = "risk_off"

    # ETH 1h confirmation: reduce conviction if BTC and ETH disagree.
    # If ETH data is insufficient for a reliable bias, we cannot complete
    # the joint confirmation — return "unknown" to trigger stronger_confirmation.
    eth_bias_1h = _bias_from_candles(eth_1h, "ETH_1h") if len(eth_1h) >= 21 else None
    if eth_bias_1h is None:
        return "unknown"
    if eth_bias_1h in {"bullish", "bearish"}:
        # Abstract phase direction:
        #   risk_on / rebound → bullish direction
        #   risk_off / selloff → bearish direction
        # If ETH 1h disagrees with the phase direction, downgrade to "transition"
        bullish_phases = {"risk_on", "rebound"}
        bearish_phases = {"risk_off", "selloff"}
        if phase in bullish_phases and eth_bias_1h == "bearish":
            return "transition"
        if phase in bearish_phases and eth_bias_1h == "bullish":
            return "transition"

    return phase


def _breadth_score(
    repo: CryptoGuardRepository,
    symbols: list[str],
    analysis_time_utc: int,
    sample_limit: int = 10,
) -> float:
    """Calculate breadth: proportion of symbols aligned with BTC direction."""
    if not symbols:
        return 0.0

    btc_1h = _load_candles(repo, "BTCUSDT", "1h", analysis_time_utc, limit=12)
    if len(btc_1h) < 6:
        return 0.0

    btc_closes = [float(c["close"]) for c in btc_1h]
    btc_change = (btc_closes[-1] - btc_closes[0]) / btc_closes[0] if btc_closes[0] else 0

    sample = [s for s in symbols if s not in LEADER_SYMBOLS][:sample_limit]
    if not sample:
        return 0.0

    aligned = 0
    total = 0
    for sym in sample:
        sym_1h = _load_candles(repo, sym, "1h", analysis_time_utc, limit=12)
        if len(sym_1h) < 6:
            continue
        sym_closes = [float(c["close"]) for c in sym_1h]
        sym_change = (sym_closes[-1] - sym_closes[0]) / sym_closes[0] if sym_closes[0] else 0
        total += 1
        if (btc_change > 0 and sym_change > 0) or (btc_change < 0 and sym_change < 0):
            aligned += 1

    if total == 0:
        return 0.0
    # Scale to -1..+1: 100% aligned = +1, 50% = 0, 0% = -1
    ratio = aligned / total
    return (ratio - 0.5) * 2.0


def _volatility_state(candles: list[dict[str, Any]]) -> str:
    """Classify volatility: normal, elevated, spike."""
    if len(candles) < 6:
        return "normal"

    ranges_pct = []
    for c in candles:
        rng = float(c["high"]) - float(c["low"])
        close = float(c["close"])
        if close > 0:
            ranges_pct.append(rng / close * 100)

    if not ranges_pct:
        return "normal"

    avg_range = sum(ranges_pct) / len(ranges_pct)
    latest = ranges_pct[-1]

    if latest > avg_range * 2.5:
        return "spike"
    if latest > avg_range * 1.5 or avg_range > 1.5:
        return "elevated"
    return "normal"


def _relative_strength(
    sym_4h: list[dict[str, Any]],
    btc_4h: list[dict[str, Any]],
    sym_1h: list[dict[str, Any]],
    btc_1h: list[dict[str, Any]],
) -> str:
    """Compare symbol performance vs BTC to classify relative strength."""
    if not sym_4h or not btc_4h:
        return "neutral"

    sym_closes = [float(c["close"]) for c in sym_4h]
    btc_closes = [float(c["close"]) for c in btc_4h]

    if not sym_closes or not btc_closes or sym_closes[0] == 0 or btc_closes[0] == 0:
        return "neutral"

    sym_return = (sym_closes[-1] - sym_closes[0]) / sym_closes[0]
    btc_return = (btc_closes[-1] - btc_closes[0]) / btc_closes[0]
    relative = sym_return - btc_return

    # Also check 1h for short-term divergence
    sym_1h_confirmation = 0
    if sym_1h and btc_1h:
        s1 = [float(c["close"]) for c in sym_1h[-6:]]
        b1 = [float(c["close"]) for c in btc_1h[-6:]]
        if s1 and b1 and s1[0] and b1[0]:
            sym_1h_ret = (s1[-1] - s1[0]) / s1[0]
            btc_1h_ret = (b1[-1] - b1[0]) / b1[0]
            sym_1h_confirmation = sym_1h_ret - btc_1h_ret

    if relative > 0.01 and sym_1h_confirmation > 0:
        return "strong"
    elif relative < -0.01 and sym_1h_confirmation < 0:
        return "weak"
    else:
        return "neutral"


def _regime_alignment(
    market_phase: str,
    btc_bias: str,
    eth_bias: str,
    symbol_rs: str,
    decision_side: str,
    breadth_score: float,
    independent_trend_cfg: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Determine if the decision side aligns with the market regime.

    Args:
        independent_trend_cfg: Config dict with keys min_relative_strength_pct,
            min_confirmations, allow_bypass. If None, uses defaults.
    """
    if not decision_side:
        return "unclear", "decision_side未提供，无法判断regime alignment"

    if market_phase == "unknown":
        return "unclear", "BTC/ETH数据不足，无法判断regime alignment"

    # Read independent_trend config with defaults
    it_cfg = independent_trend_cfg or {}
    min_confirmations = int(it_cfg.get("min_confirmations", 2))
    allow_bypass = bool(it_cfg.get("allow_bypass", True))

    side = decision_side.upper()

    # Check counter-regime
    counter = False
    if market_phase in {"rebound", "risk_on"} and side == "SHORT":
        counter = True
    elif market_phase in {"selloff", "risk_off"} and side == "LONG":
        counter = True

    if not counter:
        return "aligned", f"{side}与当前market_phase={market_phase}一致"

    # Counter-regime — check for independent_trend exception
    # Count confirmations for independent_trend
    confirmations = 0
    # Symbol rs matching the side direction counts as a confirmation
    if (symbol_rs == "strong" and side == "LONG") or (symbol_rs == "weak" and side == "SHORT"):
        confirmations += 1
    if abs(breadth_score) < 0.5:
        confirmations += 1

    if allow_bypass and symbol_rs == "strong" and side == "LONG" and confirmations >= min_confirmations:
        return "independent_trend", (
            f"个币相对强势（rs={symbol_rs}），且板块宽度不一致（breadth={breadth_score:+.2f}），"
            f"虽有market_phase={market_phase}但仍允许独立行情"
        )
    if allow_bypass and symbol_rs == "weak" and side == "SHORT" and confirmations >= min_confirmations:
        return "independent_trend", (
            f"个币相对弱势（rs={symbol_rs}），且板块宽度不一致（breadth={breadth_score:+.2f}），"
            f"虽有market_phase={market_phase}但仍允许独立行情"
        )

    return "counter_regime", (
        f"{side}方向与market_phase={market_phase}（btc={btc_bias}, eth={eth_bias}）反向，"
        f"且个币rs={symbol_rs}未达到独立行情标准"
    )


def _percentile_rank(sorted_values: list[float], value: float) -> float:
    if not sorted_values:
        return 0.0
    below = len([x for x in sorted_values if x <= value])
    return below / len(sorted_values)


def _recent_wick_count(candles: list[dict[str, Any]]) -> int:
    count = 0
    for candle in candles:
        high = float(candle["high"])
        low = float(candle["low"])
        open_price = float(candle["open"])
        close = float(candle["close"])
        spread = high - low
        if spread <= 0:
            continue
        body = abs(close - open_price)
        wick = spread - body
        if wick / spread >= 0.72:
            count += 1
    return count
