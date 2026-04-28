"""Risk management: position sizing, daily loss limit, kill switch, market hours,
PDT tracking, sanity checks. All gates run synchronously before order submission.
"""
import os
from dataclasses import dataclass
from datetime import date
from typing import Optional

from loguru import logger


STYLE_PRESETS: dict[str, dict] = {
    "conservative": {
        "max_position_pct": 0.01,
        "stop_loss_pct": 0.03,
        "take_profit_pct": 0.06,
        "daily_loss_limit_pct": 0.015,
        "max_total_positions": 5,
        "min_score_to_trade": 5,
    },
    "moderate": {
        "max_position_pct": 0.02,
        "stop_loss_pct": 0.05,
        "take_profit_pct": 0.10,
        "daily_loss_limit_pct": 0.03,
        "max_total_positions": 10,
        "min_score_to_trade": 3,
    },
    "aggressive": {
        "max_position_pct": 0.04,
        "stop_loss_pct": 0.07,
        "take_profit_pct": 0.20,
        "daily_loss_limit_pct": 0.05,
        "max_total_positions": 15,
        "min_score_to_trade": 2,
    },
}


@dataclass
class OrderParams:
    qty: int
    entry_price_estimate: float
    stop_loss_price: float
    take_profit_price: float
    notional: float


class RiskManager:
    def __init__(self, settings, config, repo):
        self.settings = settings
        self.config = config
        self.repo = repo
        self._daily_start_equity: Optional[float] = None
        self._daily_start_date: Optional[date] = None

    # ── kill switch (file-based, survives container/UI/db crashes) ────────────

    def kill_switch_active(self) -> bool:
        return os.path.exists(self.settings.kill_switch_path)

    def activate_kill_switch(self) -> None:
        path = self.settings.kill_switch_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("activated\n")
        logger.critical("KILL SWITCH ACTIVATED")

    def deactivate_kill_switch(self) -> None:
        path = self.settings.kill_switch_path
        if os.path.exists(path):
            os.remove(path)
        logger.warning("Kill switch deactivated")

    # ── live-trading gate (requires 3 independent conditions) ─────────────────

    def live_trading_allowed(self) -> bool:
        env_ok = self.settings.allow_live_trading is True
        config_ok = self.config.trading.get("mode", "paper") == "live"
        broker_ok = not self.settings.alpaca_paper
        return env_ok and config_ok and broker_ok

    def trading_mode(self) -> str:
        return "live" if self.live_trading_allowed() else "paper"

    # ── trading style ─────────────────────────────────────────────────────────

    def trading_style(self) -> str:
        return self.config.trading.get("style", "moderate")

    def effective_risk(self) -> dict:
        """Config risk values with style preset applied on top."""
        preset = STYLE_PRESETS.get(self.trading_style(), STYLE_PRESETS["moderate"])
        return {**self.config.risk, **preset}

    def min_score_for_style(self, current_equity: Optional[float] = None) -> int:
        base = int(self.effective_risk()["min_score_to_trade"])
        if current_equity is not None:
            progress = self.daily_profit_progress(current_equity)
            # Once 60% of daily goal is achieved, nudge threshold down by 1 to
            # allow more signals through — "push a little" without sizing up
            if progress >= 0.6:
                return max(1, base - 1)
        return base

    # ── daily profit goal ─────────────────────────────────────────────────────

    def daily_profit_goal(self) -> float:
        return float(self.config.risk.get("daily_profit_goal", 0.0))

    def daily_profit_progress(self, current_equity: float) -> float:
        """Returns 0.0–1.0+ progress toward daily goal; 0.0 when goal is disabled."""
        goal = self.daily_profit_goal()
        if goal <= 0 or self._daily_start_equity is None:
            return 0.0
        daily_gain = current_equity - self._daily_start_equity
        return max(0.0, daily_gain / goal)

    def daily_profit_goal_reached(self, current_equity: float) -> bool:
        return self.daily_profit_goal() > 0 and self.daily_profit_progress(current_equity) >= 1.0

    # ── daily loss limit ──────────────────────────────────────────────────────

    async def initialize_daily_baseline(self, current_equity: float) -> None:
        today = date.today()
        if self._daily_start_date != today:
            self._daily_start_equity = current_equity
            self._daily_start_date = today
            await self.repo.upsert_daily_start(today, current_equity)
            logger.info(f"Daily baseline set: equity=${current_equity:.2f}")

    def daily_loss_breached(self, current_equity: float) -> bool:
        if self._daily_start_equity is None or self._daily_start_equity <= 0:
            return False
        limit_pct = float(self.effective_risk()["daily_loss_limit_pct"])
        loss_pct = (self._daily_start_equity - current_equity) / self._daily_start_equity
        if loss_pct >= limit_pct:
            logger.warning(
                f"Daily loss {loss_pct*100:.2f}% exceeds limit {limit_pct*100:.2f}%"
            )
            return True
        return False

    @property
    def daily_start_equity(self) -> Optional[float]:
        return self._daily_start_equity

    # ── PDT tracking ──────────────────────────────────────────────────────────

    async def can_day_trade(self) -> tuple[bool, str]:
        account_type = self.config.pdt.get("account_type", "margin_under_25k")
        if account_type in ("cash", "margin_over_25k"):
            return True, "exempt"
        max_dt = int(self.config.pdt.get("max_day_trades", 3))
        count = await self.repo.count_day_trades_last_5_days()
        if count >= max_dt:
            return False, f"PDT limit reached ({count}/{max_dt} in last 5 days)"
        return True, f"{count}/{max_dt}"

    # ── sanity / pre-trade filters ────────────────────────────────────────────

    def passes_sanity_filters(self, signal) -> tuple[bool, str]:
        rmin = float(self.config.risk.get("min_price", 1.0))
        rmax = float(self.config.risk.get("max_price", 1000.0))
        if signal.price < rmin:
            return False, f"price ${signal.price:.2f} below min ${rmin:.2f}"
        if signal.price > rmax:
            return False, f"price ${signal.price:.2f} above max ${rmax:.2f}"
        if signal.rsi > 80:
            return False, f"extreme RSI {signal.rsi:.1f}"
        return True, "ok"

    # ── position sizing for autonomous trades ─────────────────────────────────

    def size_auto_order(
        self, signal, account: dict, reference_price: Optional[float] = None
    ) -> Optional[OrderParams]:
        equity = float(account.get("equity", 0))
        if equity <= 0:
            logger.warning("Cannot size order: zero equity")
            return None

        risk = self.effective_risk()
        max_pct = float(risk["max_position_pct"])
        notional = equity * max_pct

        bp = float(account.get("buying_power", 0))
        notional = min(notional, bp * 0.95)
        if notional < signal.price:
            logger.info(f"{signal.symbol}: notional ${notional:.2f} < price ${signal.price:.2f}, skip")
            return None

        qty = int(notional / signal.price)
        if qty < 1:
            return None

        sl_pct = float(risk["stop_loss_pct"])
        tp_pct = float(risk["take_profit_pct"])
        # Anchor SL/TP to the live price so bracket legs pass Alpaca's validation
        price_ref = reference_price if reference_price is not None else signal.price
        stop_loss = round(price_ref * (1 - sl_pct), 2)
        take_profit = round(price_ref * (1 + tp_pct), 2)

        return OrderParams(
            qty=qty,
            entry_price_estimate=signal.price,
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            notional=qty * signal.price,
        )

    # ── manual trade sizing (user picks the dollar amount) ────────────────────

    def size_manual_order(
        self, *, price: float, dollar_amount: float, buying_power: float
    ) -> Optional[OrderParams]:
        if price <= 0 or dollar_amount <= 0:
            return None
        notional = min(dollar_amount, buying_power * 0.95)
        qty = int(notional / price)
        if qty < 1:
            return None
        risk = self.effective_risk()
        sl_pct = float(risk["stop_loss_pct"])
        tp_pct = float(risk["take_profit_pct"])
        return OrderParams(
            qty=qty,
            entry_price_estimate=price,
            stop_loss_price=round(price * (1 - sl_pct), 2),
            take_profit_price=round(price * (1 + tp_pct), 2),
            notional=qty * price,
        )
