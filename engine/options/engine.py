"""OptionsEngine — autonomous options trading loop.

Runs as a parallel asyncio task to TradingEngine. Shares RiskManager,
AlpacaClient, Repository via constructor injection.

Tick sequence (every options.scan_interval_seconds):
  1. Engine paused?  → skip
  2. Kill switch?    → skip
  3. Market closed?  → skip
  4. Establish daily baseline
  5. Daily loss breached?   → pause
  6. Daily profit goal hit? → pause
  7. Build watchlist
  8. Scan symbols → OptionsSignal
  9. For each non-neutral signal above threshold:
     - Skip if already holding option in this underlying
     - Skip if max_option_positions reached
     - Select contract; size; write pending row; submit limit; update row
"""
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from data.alpaca_client import AlpacaClient
from options.contract_selector import ContractSelector, SelectorConfig
from options.position_monitor import MonitorConfig, PositionMonitor
from options.strategy import OptionsSignal, score_options_signal
from risk.manager import RiskManager


class OptionsEngine:
    def __init__(self, settings, config, client: AlpacaClient, risk: RiskManager, repo):
        self.settings = settings
        self.config = config
        self.client = client
        self.risk = risk
        self.repo = repo
        self._stop_event = asyncio.Event()
        self._paused = False
        self._running = False
        self._monitor: Optional[PositionMonitor] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._last_signals: list[OptionsSignal] = []
        self._last_scan_at: Optional[str] = None
        self._last_error: Optional[str] = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True
        logger.warning("Options engine paused")

    def resume(self) -> None:
        self._paused = False
        logger.info("Options engine resumed")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._monitor:
            self._monitor.stop()
        if self._monitor_task:
            self._monitor_task.cancel()
        self._running = False

    async def run(self) -> None:
        if not self.config.options.get("enabled", False):
            logger.info("Options engine disabled in config — exiting run()")
            return

        errors = self.config.validate_options_config()
        if errors:
            for err in errors:
                logger.error(f"options config invalid: {err}")
                await self.repo.log_event("options_config_error", err)
            self._paused = True
            return

        self._running = True
        await self.repo.log_event(
            "options_engine_started",
            f"mode={self.risk.trading_mode()}",
        )
        logger.info(f"Options engine running (mode={self.risk.trading_mode()})")

        self._monitor = PositionMonitor(
            self.client, self.repo, self._build_monitor_config()
        )
        monitor_interval = int(self.config.options.get("monitor_interval_seconds", 60))
        self._monitor_task = asyncio.create_task(
            self._monitor.run(monitor_interval), name="options_position_monitor",
        )

        scan_interval = int(self.config.options.get("scan_interval_seconds", 300))
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as e:
                logger.exception(f"Options tick failed: {e}")
                self._last_error = str(e)
                await self.repo.log_event("options_tick_error", str(e))
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=scan_interval,
                )
            except asyncio.TimeoutError:
                pass

        self._running = False
        await self.repo.log_event("options_engine_stopped", "shutdown")
        logger.info("Options engine stopped")

    def _build_monitor_config(self) -> MonitorConfig:
        opts = self.config.options
        lo = opts.get("limit_order", {})
        return MonitorConfig(
            profit_target_pct=float(opts["profit_target_pct"]),
            stop_loss_pct=float(opts["stop_loss_pct"]),
            dte_floor=int(opts["dte_floor"]),
            fill_timeout_seconds=int(lo.get("fill_timeout_seconds", 120)),
            exit_retry_max=int(lo.get("exit_retry_max", 3)),
            exit_retry_price_step_pct=float(lo.get("exit_retry_price_step_pct", 0.05)),
        )

    def _build_selector_config(self) -> SelectorConfig:
        opts = self.config.options
        liq = opts.get("liquidity", {})
        return SelectorConfig(
            target_delta=float(opts["target_delta"]),
            delta_tolerance=float(opts["delta_tolerance"]),
            min_dte=int(opts["min_dte"]),
            max_dte=int(opts["max_dte"]),
            max_spread_pct=float(liq.get("max_spread_pct", 0.20)),
            min_volume=int(liq.get("min_volume", 10)),
            min_open_interest=int(liq.get("min_open_interest", 100)),
            dte_floor=int(opts["dte_floor"]),
        )

    # ── tick ──────────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        if self._paused:
            logger.debug("options engine paused — skip tick")
            return
        if self.risk.kill_switch_active():
            return
        if not await self.client.is_market_open():
            return

        account = await self.client.get_account()
        await self.risk.initialize_daily_baseline(float(account["equity"]))

        if self.risk.daily_loss_breached(float(account["equity"])):
            await self.repo.log_event(
                "options_daily_limit_breached",
                f"equity=${account['equity']:.2f}",
            )
            self._paused = True
            return

        if self.risk.daily_profit_goal_reached(float(account["equity"])):
            await self.repo.log_event(
                "options_daily_goal_reached",
                f"equity=${account['equity']:.2f}",
            )
            self._paused = True
            return

        if account.get("trading_blocked") or account.get("account_blocked"):
            self._paused = True
            await self.repo.log_event("options_account_blocked", str(account))
            return

        await self._scan_and_execute(account)

    # ── scan and execute ──────────────────────────────────────────────────────

    async def _scan_and_execute(self, account: dict) -> None:
        watchlist = await self._build_watchlist()
        if not watchlist:
            logger.warning("options: empty watchlist — skip tick")
            return

        open_trades = await self.repo.list_open_option_trades()
        held_underlyings = {t["underlying_symbol"] for t in open_trades}
        max_positions = int(self.config.options.get("max_option_positions", 5))

        timeframe = self.config.options.get("bar_timeframe", "5Min")
        lookback = int(self.config.options.get("lookback_bars", 78))
        min_score = int(self.config.options.get("min_score_to_trade", 3))

        signals: list[OptionsSignal] = []
        for sym in watchlist:
            if self._stop_event.is_set():
                return
            try:
                df = await self.client.get_bars(sym, timeframe=timeframe, limit=lookback)
            except Exception as e:
                logger.warning(f"options bars fetch {sym} failed: {e}")
                continue
            if df is None or len(df) < 30:
                continue
            sig = score_options_signal(sym, df, min_score=min_score)
            signals.append(sig)

        tradable = [s for s in signals if s.direction != "neutral"]
        tradable.sort(key=lambda s: s.score, reverse=True)
        self._last_signals = tradable
        self._last_scan_at = datetime.now(timezone.utc).isoformat()

        if not tradable:
            return

        selector = ContractSelector(self.client, self._build_selector_config())

        for sig in tradable:
            if self._stop_event.is_set() or self.risk.kill_switch_active():
                break
            if len(held_underlyings) >= max_positions:
                logger.info(
                    f"options: max_option_positions={max_positions} reached, "
                    f"skipping {sig.symbol}"
                )
                break
            if sig.symbol in held_underlyings:
                continue

            await self._execute_signal(sig, account, selector, held_underlyings)
            account = await self.client.get_account()

    async def _build_watchlist(self) -> list[str]:
        default = list(self.config.trading.get("watchlist", []))
        screener_cfg = self.config.screener
        if screener_cfg.get("enabled", False):
            try:
                actives = await self.client.get_most_actives(
                    top=int(screener_cfg.get("top_n", 50))
                )
                if screener_cfg.get("include_gainers", True):
                    gainers = await self.client.get_top_gainers(top=25)
                    actives = list(dict.fromkeys(actives + gainers))
                actives = await self.client.filter_symbols_by_price(
                    actives,
                    min_price=float(screener_cfg.get("min_price", 5.0)),
                    max_price=float(screener_cfg.get("max_price", 500.0)),
                )
                return actives or default
            except Exception as e:
                logger.warning(f"options screener fallback to static: {e}")
                return default
        return default

    async def _execute_signal(
        self,
        sig: OptionsSignal,
        account: dict,
        selector: ContractSelector,
        held_underlyings: set[str],
    ) -> None:
        contract = await selector.select(sig.symbol, sig.direction)
        if contract is None:
            await self.repo.log_event(
                "options_signal_no_eligible_contract",
                f"{sig.symbol} direction={sig.direction} score={sig.score}",
            )
            return

        equity = float(account.get("equity", 0))
        bp = float(account.get("buying_power", 0))
        max_pct = float(self.config.options.get("max_position_pct", 0.02))
        notional = min(equity * max_pct, bp * 0.95)
        per_contract_cost = contract.mid * 100
        if per_contract_cost <= 0:
            return
        qty = int(notional / per_contract_cost)
        if qty < 1:
            await self.repo.log_event(
                "options_skipped_sizing",
                f"{sig.symbol}: notional ${notional:.2f} < contract ${per_contract_cost:.2f}",
            )
            return

        client_order_id = f"opt_auto_{sig.symbol}_{uuid.uuid4().hex[:8]}"

        await self.repo.insert_option_trade_pending(
            client_order_id=client_order_id,
            contract_symbol=contract.contract_symbol,
            underlying_symbol=contract.underlying_symbol,
            contract_type=contract.contract_type,
            expiration_date=contract.expiration_date,
            strike_price=contract.strike_price,
            side="buy",
            qty=qty,
            trading_mode=self.risk.trading_mode(),
        )

        try:
            result = await self.client.submit_option_limit_order(
                contract_symbol=contract.contract_symbol,
                qty=qty,
                side="buy",
                limit_price=contract.mid,
                client_order_id=client_order_id,
            )
            await self.repo.update_option_trade_after_submit(
                client_order_id=client_order_id,
                broker_order_id=result["id"],
                status=result["status"],
            )
            premium_paid = contract.mid * 100 * qty
            await self.repo.update_option_trade_with_entry_data(
                client_order_id=client_order_id,
                entry_delta=contract.delta,
                entry_gamma=contract.gamma,
                entry_theta=contract.theta,
                entry_vega=contract.vega,
                entry_iv=contract.iv,
                entry_bid=contract.bid,
                entry_ask=contract.ask,
                entry_mid=contract.mid,
                premium_paid=premium_paid,
                dte_at_entry=contract.dte,
                underlying_score=sig.score,
                underlying_signals=sig.signals,
            )
            await self.repo.log_event(
                "option_auto_entry",
                f"{contract.contract_symbol} buy x{qty} @${contract.mid:.2f} "
                f"(score={sig.score} delta={contract.delta:.2f} dte={contract.dte})",
            )
            held_underlyings.add(contract.underlying_symbol)
            logger.info(
                f"OPTION BUY {contract.contract_symbol} x{qty} @${contract.mid:.2f} "
                f"({sig.symbol} {sig.direction} score={sig.score}, "
                f"delta={contract.delta:.2f}, dte={contract.dte})"
            )
        except Exception as e:
            logger.error(f"Option entry failed for {contract.contract_symbol}: {e}")
            await self.repo.update_option_trade_after_submit(
                client_order_id=client_order_id,
                broker_order_id=None,
                status="error",
            )
            await self.repo.log_event(
                "option_auto_entry_error",
                f"{contract.contract_symbol}: {e}",
            )

    def snapshot_status(self) -> dict:
        return {
            "running": self._running,
            "paused": self._paused,
            "last_scan_at": self._last_scan_at,
            "last_error": self._last_error,
            "open_signals_count": len(self._last_signals),
        }
