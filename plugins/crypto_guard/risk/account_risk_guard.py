"""Account-level risk guard: drawdown-based risk_off, hard_risk_off, and daily_loss_pause.

Three-tier risk mode:
1. risk_off: drawdown <= -2.5% → reduce risk_percent, block bad symbol+side combos
2. hard_risk_off: drawdown <= -3.0% → block ALL new paper orders
3. daily_loss_pause: 2 consecutive -1R or daily avg_r <= -0.5 → block ALL new paper orders

Recovery: wait 24h + last 10 avg_r > 0 + loss_count <= 4
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.storage.repository import CryptoGuardRepository

LOGGER = get_logger("crypto_guard.account_risk_guard")

DEFAULTS = {
    "drawdown_risk_off_threshold": -2.5,
    "drawdown_hard_risk_off_threshold": -3.0,
    "risk_off_risk_percent": 0.25,
    "recovery_min_avg_r": 0.0,
    "recovery_max_loss_count": 4,
    "recovery_lookback": 10,
    "recovery_wait_hours": 24,
    "daily_loss_pause_consecutive_losses": 2,
    "daily_loss_pause_avg_r_threshold": -0.5,
    "cooldown_symbols": {
        "BTCUSDT_LONG": 48,
        "LTCUSDT_LONG": 48,
        "ETHUSDT_LONG": 48,
        "BNBUSDT_SHORT": 48,
    },
    # FS-2: combo avg-R blocking on a healthy account is OPT-IN.
    # Default False preserves the established healthy-account behavior.
    # When False, a negative symbol-side avgR alone does NOT block a
    # healthy account. Cooldown / negative avgR still block during
    # ordinary risk_off and during recovery.
    "block_healthy_account_on_negative_combo_avg_r": False,
    # 08-10 P2-3 (reviewer finding): per-trade and total account risk caps.
    # prd.md P1-3: effective risk may never exceed the configured per-trade
    # cap nor the remaining total-account budget. These feed the risk
    # adjustment verifier's FAIL-CLOSED cap gate through
    # run_ga_workers._derive_account_state.
    "max_single_trade_risk_pct": 2.0,
    "max_total_risk_pct": 10.0,
}


def _load_account_risk_config() -> dict[str, Any]:
    try:
        cfg = load_config().trading_mode
        return {**DEFAULTS, **cfg.get("account_risk", {})}
    except Exception:
        return dict(DEFAULTS)


class AccountRiskGuard:
    def __init__(self, repo: CryptoGuardRepository, *, now_provider: Callable[[], datetime] | None = None):
        self.repo = repo
        self._config = _load_account_risk_config()
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    @property
    def drawdown_threshold(self) -> float:
        return float(self._config.get("drawdown_risk_off_threshold", DEFAULTS["drawdown_risk_off_threshold"]))

    @property
    def hard_risk_off_threshold(self) -> float:
        return float(self._config.get("drawdown_hard_risk_off_threshold", DEFAULTS["drawdown_hard_risk_off_threshold"]))

    @property
    def risk_off_risk_percent(self) -> float:
        return float(self._config.get("risk_off_risk_percent", DEFAULTS["risk_off_risk_percent"]))

    @property
    def recovery_wait_hours(self) -> int:
        return int(self._config.get("recovery_wait_hours", DEFAULTS["recovery_wait_hours"]))

    @property
    def cooldown_symbols(self) -> dict[str, int]:
        raw = self._config.get("cooldown_symbols", {})
        return {str(k): int(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    @property
    def block_healthy_on_negative_combo_avg_r(self) -> bool:
        """FS-2: opt-in flag. Default False — a negative symbol-side avgR
        alone must NOT permanently block a healthy account."""
        return bool(self._config.get(
            "block_healthy_account_on_negative_combo_avg_r",
            DEFAULTS["block_healthy_account_on_negative_combo_avg_r"],
        ))

    def check(self, *, symbol: str, side: str) -> dict[str, Any]:
        account = self._get_account()
        if not account:
            return _ok_result(drawdown_pct=0.0)

        drawdown_pct = _drawdown_percent(account)
        recovery = self._check_recovery()
        recovery_eligible = recovery.get("eligible", False)

        # Check daily loss pause
        daily_pause = self._check_daily_loss_pause()
        daily_pause_active = daily_pause.get("active", False)
        daily_pause_reason = daily_pause.get("reason")

        # Check hard_risk_off (drawdown <= -3.0%)
        hard_risk_off = drawdown_pct <= self.hard_risk_off_threshold

        # Check risk_off (drawdown <= -2.5%)
        risk_off = drawdown_pct <= self.drawdown_threshold

        # Determine pause state
        pause_active = hard_risk_off or daily_pause_active
        pause_reason = None
        if hard_risk_off:
            pause_reason = f"hard_risk_off：账户回撤 {drawdown_pct:.2f}% 超过 {self.hard_risk_off_threshold}% 阈值"
        elif daily_pause_active:
            pause_reason = daily_pause_reason

        # ALWAYS compute cooldown and combo_avg_r — they are needed even in
        # risk_off (non-hard) mode to block specific symbol+side combos.
        combo_key = f"{symbol}_{side}".upper()
        cooldown_hours = self.cooldown_symbols.get(combo_key, 0)
        cooldown_active = False
        cooldown_until = None
        blocked = False
        blocked_reason = None

        if cooldown_hours > 0:
            last_loss_time = self._last_loss_time_for_combo(symbol, side)
            if last_loss_time:
                now = self._now_provider()
                cooldown_end = last_loss_time + timedelta(hours=cooldown_hours)
                if now < cooldown_end:
                    cooldown_active = True
                    cooldown_until = cooldown_end.isoformat()
                    blocked = True
                    blocked_reason = f"{combo_key} 冷却中（{cooldown_hours}h），上次亏损: {last_loss_time.strftime('%m-%d %H:%M')} UTC"

        combo_avg_r = self._combo_avg_r(symbol, side, lookback=20)
        # FS-2: combo avg-R blocking on a healthy account is opt-in.
        # When neither risk_off nor pause is active, a negative avgR alone
        # does NOT block unless block_healthy_on_negative_combo_avg_r=True.
        # During risk_off (non-hard) and during recovery, negative avgR
        # remains blocking.
        combo_avg_r_block_eligible = (
            combo_avg_r is not None and combo_avg_r < 0 and not blocked
        )
        healthy_path = (not risk_off) and (not pause_active)
        if combo_avg_r_block_eligible:
            if healthy_path and not self.block_healthy_on_negative_combo_avg_r:
                # FS-2: preserve established healthy-account behavior.
                # Negative avgR alone is informational on a healthy account.
                pass
            else:
                blocked = True
                blocked_reason = f"{combo_key} 历史 avg_r={combo_avg_r:.3f} < 0，禁止开仓"

        # Early return: not in any risk-off territory — but still honor combo
        # cooldown blocks (and negative avgR if opt-in).
        if healthy_path:
            if blocked:
                return {
                    "ok": True,
                    "risk_off": False,
                    "hard_risk_off": False,
                    "daily_loss_pause": False,
                    "pause_active": False,
                    "pause_reason": None,
                    "drawdown_pct": drawdown_pct,
                    "effective_risk_percent": None,
                    "blocked": True,
                    "blocked_reason": blocked_reason,
                    "cooldown_active": cooldown_active,
                    "cooldown_until": cooldown_until,
                    "recovery_eligible": False,
                    "recovery_status": {},
                    "daily_pause_status": daily_pause,
                }
            return _ok_result(drawdown_pct=drawdown_pct)

        # In risk_off but not hard/pause — check recovery
        if not hard_risk_off and not daily_pause_active:
            if recovery_eligible:
                # Recovery lifts the risk_off tier, but combo cooldown /
                # negative avg_r blocks MUST still be honored.
                if blocked:
                    return {
                        "ok": True,
                        "risk_off": False,
                        "hard_risk_off": False,
                        "daily_loss_pause": False,
                        "pause_active": False,
                        "pause_reason": None,
                        "drawdown_pct": drawdown_pct,
                        "effective_risk_percent": None,
                        "blocked": True,
                        "blocked_reason": blocked_reason,
                        "cooldown_active": cooldown_active,
                        "cooldown_until": cooldown_until,
                        "recovery_eligible": True,
                        "recovery_status": recovery,
                        "daily_pause_status": daily_pause,
                    }
                return _ok_result(drawdown_pct=drawdown_pct)
            recovery_reason = recovery.get("reason")
            if not recovery_reason:
                recovery_reason = (
                    f"avg_r={recovery.get('avg_r', 0):.3f}, "
                    f"亏损{recovery.get('loss_count', 0)}笔"
                )
            return {
                "ok": True,
                "risk_off": True,
                "hard_risk_off": False,
                "daily_loss_pause": False,
                "pause_active": False,
                "pause_reason": f"账户回撤 {drawdown_pct:.2f}% 处于 risk_off，恢复条件未满足（{recovery_reason}）",
                "drawdown_pct": drawdown_pct,
                "effective_risk_percent": self.risk_off_risk_percent,
                "blocked": blocked,
                "blocked_reason": blocked_reason,
                "cooldown_active": cooldown_active,
                "cooldown_until": cooldown_until,
                "recovery_eligible": False,
                "recovery_status": recovery,
                "daily_pause_status": daily_pause,
            }

        # We are in hard_risk_off or daily_loss_pause territory
        risk_off_reason = f"账户回撤 {drawdown_pct:.2f}% 超过阈值 {self.drawdown_threshold}%"

        result = {
            "ok": True,
            "risk_off": risk_off,
            "hard_risk_off": hard_risk_off,
            "daily_loss_pause": daily_pause_active,
            "pause_active": pause_active,
            "pause_reason": pause_reason,
            "drawdown_pct": drawdown_pct,
            "effective_risk_percent": self.risk_off_risk_percent,
            "blocked": blocked or pause_active,
            "blocked_reason": pause_reason if pause_active else blocked_reason,
            "cooldown_active": cooldown_active,
            "cooldown_until": cooldown_until,
            "recovery_eligible": recovery_eligible,
            "recovery_status": recovery,
            "daily_pause_status": daily_pause,
        }

        LOGGER.info(
            "account_risk_guard: risk_off=%s hard_risk_off=%s daily_pause=%s drawdown=%.2f%% blocked=%s",
            risk_off, hard_risk_off, daily_pause_active, drawdown_pct, result["blocked"],
        )
        return result

    def _get_account(self) -> dict[str, Any] | None:
        row = self.repo.conn.execute(
            "SELECT * FROM paper_accounts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def _check_daily_loss_pause(self) -> dict[str, Any]:
        """Check if daily loss pause should activate.

        Triggers:
        - 2 consecutive stop losses today (pnl_r <= -1.0)
        - Today's avg_r <= -0.5
        """
        # "Today" in user-facing risk alerts follows UTC+8, matching Feishu reports.
        utc8 = timezone(timedelta(hours=8))
        now_local = self._now_provider().astimezone(utc8)
        today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        today_start_iso = today_start.isoformat()

        rows = self.repo.conn.execute(
            """
            SELECT pnl_r, closed_at FROM paper_trades
            WHERE pnl_r IS NOT NULL AND closed_at IS NOT NULL AND closed_at >= %s::timestamptz
            ORDER BY closed_at DESC
            """,
            (today_start_iso,),
        ).fetchall()

        if not rows:
            return {"active": False, "reason": None, "today_trades": 0}

        values = [float(r["pnl_r"]) for r in rows]
        avg_r = sum(values) / len(values)

        # Check consecutive stop losses (pnl_r <= -1.0)
        consec_threshold = int(self._config.get("daily_loss_pause_consecutive_losses", DEFAULTS["daily_loss_pause_consecutive_losses"]))
        consec_losses = 0
        for v in values:  # sorted DESC by time, so most recent first
            if v <= -1.0:
                consec_losses += 1
            else:
                break

        avg_r_threshold = float(self._config.get("daily_loss_pause_avg_r_threshold", DEFAULTS["daily_loss_pause_avg_r_threshold"]))

        if consec_losses >= consec_threshold:
            return {
                "active": True,
                "reason": f"daily_loss_pause：今日连续 {consec_losses} 笔止损（pnl_r <= -1.0）",
                "today_trades": len(values),
                "avg_r": avg_r,
                "consec_losses": consec_losses,
            }

        if avg_r <= avg_r_threshold:
            return {
                "active": True,
                "reason": f"daily_loss_pause：今日 avg_r={avg_r:.3f} <= {avg_r_threshold}",
                "today_trades": len(values),
                "avg_r": avg_r,
                "consec_losses": 0,
            }

        return {
            "active": False,
            "reason": None,
            "today_trades": len(values),
            "avg_r": avg_r,
            "consec_losses": 0,
        }

    def _last_loss_time_for_combo(self, symbol: str, side: str) -> datetime | None:
        row = self.repo.conn.execute(
            """
            SELECT closed_at FROM paper_trades
            WHERE symbol=%s AND side=%s AND pnl_r IS NOT NULL AND pnl_r < 0
            ORDER BY closed_at DESC LIMIT 1
            """,
            (symbol, side),
        ).fetchone()
        if not row or not row["closed_at"]:
            return None
        try:
            dt = datetime.fromisoformat(str(row["closed_at"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return None

    def _combo_avg_r(self, symbol: str, side: str, lookback: int = 20) -> float | None:
        rows = self.repo.conn.execute(
            """
            SELECT pnl_r FROM paper_trades
            WHERE symbol=%s AND side=%s AND pnl_r IS NOT NULL AND closed_at IS NOT NULL
            ORDER BY closed_at DESC LIMIT %s
            """,
            (symbol, side, lookback),
        ).fetchall()
        if not rows:
            return None
        values = [float(r["pnl_r"]) for r in rows]
        return sum(values) / len(values)

    def _check_recovery(self) -> dict[str, Any]:
        lookback = int(self._config.get("recovery_lookback", DEFAULTS["recovery_lookback"]))
        min_avg_r = float(self._config.get("recovery_min_avg_r", DEFAULTS["recovery_min_avg_r"]))
        max_loss_count = int(self._config.get("recovery_max_loss_count", DEFAULTS["recovery_max_loss_count"]))
        wait_hours = self.recovery_wait_hours

        # Check if enough time has passed since last loss
        last_loss = self.repo.conn.execute(
            "SELECT closed_at FROM paper_trades WHERE pnl_r IS NOT NULL AND pnl_r < 0 AND closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 1"
        ).fetchone()
        if last_loss and last_loss["closed_at"]:
            try:
                last_loss_dt = datetime.fromisoformat(str(last_loss["closed_at"]).replace("Z", "+00:00"))
                if last_loss_dt.tzinfo is None:
                    last_loss_dt = last_loss_dt.replace(tzinfo=timezone.utc)
                if self._now_provider() - last_loss_dt < timedelta(hours=wait_hours):
                    return {"eligible": False, "reason": f"距离上次亏损不足 {wait_hours}h", "sample_count": 0}
            except (ValueError, TypeError):
                pass

        rows = self.repo.conn.execute(
            """
            SELECT pnl_r FROM paper_trades
            WHERE pnl_r IS NOT NULL AND closed_at IS NOT NULL
            ORDER BY closed_at DESC LIMIT %s
            """,
            (lookback,),
        ).fetchall()

        if not rows:
            return {"eligible": False, "reason": "no_closed_trades", "sample_count": 0}

        values = [float(r["pnl_r"]) for r in rows]
        avg_r = sum(values) / len(values)
        loss_count = len([v for v in values if v < 0])
        sample_count = len(values)

        eligible = avg_r > min_avg_r and loss_count <= max_loss_count
        return {
            "eligible": eligible,
            "avg_r": avg_r,
            "loss_count": loss_count,
            "sample_count": sample_count,
            "min_avg_r": min_avg_r,
            "max_loss_count": max_loss_count,
        }


def _drawdown_percent(account: dict[str, Any]) -> float:
    """Internal drawdown sign convention: loss is negative (<= 0).

    Matches paper/execution_quality.py:152 so external renderers can rely on a
    single sign direction. A positive (equity>initial) return is reported as
    0 — the display layer surfaces the magnitude separately when needed.
    """
    initial = float(account.get("initial_balance") or 10000.0)
    if initial <= 0:
        return 0.0
    equity = float(account.get("equity") or initial)
    raw = (equity - initial) / initial * 100.0
    return min(0.0, raw)


def _ok_result(drawdown_pct: float) -> dict[str, Any]:
    return {
        "ok": True,
        "risk_off": False,
        "hard_risk_off": False,
        "daily_loss_pause": False,
        "pause_active": False,
        "pause_reason": None,
        "drawdown_pct": drawdown_pct,
        "effective_risk_percent": None,
        "blocked": False,
        "blocked_reason": None,
        "cooldown_active": False,
        "cooldown_until": None,
        "recovery_eligible": False,
        "recovery_status": {},
        "daily_pause_status": {},
    }
