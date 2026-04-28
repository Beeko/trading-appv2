"""Main trading engine loop.

Runs as a single asyncio task alongside the FastAPI server. Each tick:
  1. Refresh kill-switch state
  2. Confirm market is open (Alpaca clock)
  3. Establish daily equity baseline if first tick of day
  4. Confirm daily loss limit not breached
  5. Confirm PDT capacity
  6. Refresh watchlist (WSB scanner if enabled, else config watchlist)
  7. Score every symbol via the active strategy
  8. For each symbol scoring above threshold:
       - Skip if already held / max positions reached
       - Risk-size the order
       - Persist pending trade row, then submit, then update status
"""
import asyncio
import uuid
from typing import Optional

from loguru import logger

from data.alpaca_client import AlpacaClient
from data.reddit_scanner import RedditScanner
from data.screener import MarketScreener
from indicators.technical import SignalScore
from risk.manager import RiskManager
from strategies.wsb_momentum import WSBMomentumStrategy


class TradingEngine:
    def __init__(self, settings, config, client: AlpacaClient, risk: RiskManager, repo):
        self.settings = settings
        self.config = config
        self.client = client
        self.risk = risk
        self.repo = repo
        self.strategy = WSBMomentumStrategy()
        self._stop_event = asyncio.Event()
        self._paused = False
        self._running = False
        self._reddit: Optional[RedditScanner] = None
        self._tradable_symbols_cache: Optional[set[str]] = None
        self._last_scan_signals: list[SignalScore] = []
        self._screener = MarketScreener(client)

        if (
            settings.reddit_client_id
            and settings.reddit_client_secret
            and config.wsb_scanner.get("enabled", False)
        ):
            self._reddit = RedditScanner(
                client_id=settings.reddit_client_id,
                client_secret=settings.reddit_client_secret,
                user_agent=settings.reddit_user_agent or "TradingBot/1.0",
            )

    # ── lifecycle ─────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True
        logger.warning("Engine paused")

    def resume(self) -> None:
        self._paused = False
        logger.info("Engine resumed")

    async def stop(self) -> None:
        self._stop_event.set()
        self._running = False

    async def run(self) -> None:
        self._running = True
        await self.repo.log_event("engine_started", f"mode={self.risk.trading_mode()}")
        logger.info(f"Trading engine running (mode={self.risk.trading_mode()})")

        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as e:
                logger.exception(f"Tick failed: {e}")
                await self.repo.log_event("tick_error", str(e))

            interval = int(self.config.trading.get("scan_interval_seconds", 60))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

        self._running = False
        await self.repo.log_event("engine_stopped", "shutdown")
        logger.info("Trading engine stopped")

    # ── tick ──────────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        if self._paused:
            logger.debug("paused — skip tick")
            return

        if self.risk.kill_switch_active():
            logger.warning("Kill switch active — skip tick")
            return

        if not await self.client.is_market_open():
            logger.debug("Market closed — skip tick")
            return

        # Establish daily baseline once per day
        account = await self.client.get_account()
        await self.risk.initialize_daily_baseline(float(account["equity"]))

        if self.risk.daily_loss_breached(float(account["equity"])):
            await self.repo.log_event(
                "daily_limit_breached",
                f"equity=${account['equity']:.2f}, start=${self.risk.daily_start_equity:.2f}",
            )
            self._paused = True
            return

        if self.risk.daily_profit_goal_reached(float(account["equity"])):
            goal = self.risk.daily_profit_goal()
            gain = float(account["equity"]) - self.risk.daily_start_equity
            await self.repo.log_event(
                "daily_goal_reached",
                f"gain=${gain:.2f} >= goal=${goal:.2f} — pausing to lock in profit",
            )
            logger.info(f"Daily profit goal ${goal:.2f} reached (gain=${gain:.2f}) — engine paused")
            self._paused = True
            return

        if account.get("trading_blocked") or account.get("account_blocked"):
            logger.error(f"Account blocked: {account}")
            await self.repo.log_event("account_blocked", str(account))
            self._paused = True
            return

        watchlist = await self._build_watchlist()
        if not watchlist:
            logger.warning("Empty watchlist — skip tick")
            return

        logger.info(f"Scanning {len(watchlist)} symbols")
        signals = await self._scan(watchlist)
        self._last_scan_signals = sorted(signals, key=lambda s: s.score, reverse=True)

        positions = await self.client.get_positions()
        held = {p["symbol"] for p in positions}
        max_pos = int(self.risk.effective_risk()["max_total_positions"])
        min_score = self.risk.min_score_for_style(float(account["equity"]))

        for sig in self._last_scan_signals:
            if self._stop_event.is_set() or self.risk.kill_switch_active():
                break
            if sig.score < min_score:
                await self.repo.insert_signal(
                    symbol=sig.symbol, score=sig.score, signals=sig.signals,
                    rsi=sig.rsi, volume_ratio=sig.volume_ratio, price=sig.price,
                    action_taken="skipped_score",
                )
                continue
            if sig.symbol in held:
                await self.repo.insert_signal(
                    symbol=sig.symbol, score=sig.score, signals=sig.signals,
                    rsi=sig.rsi, volume_ratio=sig.volume_ratio, price=sig.price,
                    action_taken="skipped_already_held",
                )
                continue
            if len(held) >= max_pos:
                await self.repo.insert_signal(
                    symbol=sig.symbol, score=sig.score, signals=sig.signals,
                    rsi=sig.rsi, volume_ratio=sig.volume_ratio, price=sig.price,
                    action_taken="skipped_max_positions",
                )
                continue

            ok, reason = self.risk.passes_sanity_filters(sig)
            if not ok:
                logger.info(f"{sig.symbol}: failed sanity ({reason})")
                await self.repo.insert_signal(
                    symbol=sig.symbol, score=sig.score, signals=sig.signals,
                    rsi=sig.rsi, volume_ratio=sig.volume_ratio, price=sig.price,
                    action_taken=f"skipped_sanity:{reason}",
                )
                continue

            await self._execute_signal(sig, account)
            held.add(sig.symbol)
            account = await self.client.get_account()  # refresh BP after order

    # ── watchlist construction ────────────────────────────────────────────────

    async def _build_watchlist(self) -> list[str]:
        default = list(self.config.trading.get("watchlist", []))

        # Screener takes priority — replaces static list when enabled
        if self.config.screener.get("enabled", False):
            cfg = self.config.screener
            try:
                symbols = await self._screener.get_symbols(
                    top_n=int(cfg.get("top_n", 50)),
                    include_gainers=bool(cfg.get("include_gainers", True)),
                    min_price=float(cfg.get("min_price", 5.0)),
                    max_price=float(cfg.get("max_price", 500.0)),
                )
                if symbols:
                    return symbols
                logger.warning("Screener returned no symbols, falling back to static watchlist")
            except Exception as e:
                logger.warning(f"Screener error, falling back to static watchlist: {e}")
            return default

        # WSB scanner merges on top of static list
        if self._reddit is not None and self.config.wsb_scanner.get("enabled", False):
            if self._tradable_symbols_cache is None:
                self._tradable_symbols_cache = await self.client.get_tradable_symbols()
            try:
                counter = await self._reddit.get_trending_tickers(
                    subreddit="wallstreetbets",
                    limit=100,
                    valid_symbols=self._tradable_symbols_cache or None,
                )
            except Exception as e:
                logger.warning(f"WSB scan failed, using default watchlist: {e}")
                return default
            min_mentions = int(self.config.wsb_scanner.get("min_mentions", 3))
            max_n = int(self.config.wsb_scanner.get("max_tickers", 25))
            trending = [t for t, c in counter.most_common(max_n) if c >= min_mentions]
            if not trending:
                return default
            logger.info(f"WSB trending: {trending}")
            merged = list(dict.fromkeys(trending + default))[:max_n]
            return merged

        return default

    # ── scoring ───────────────────────────────────────────────────────────────

    async def _scan(self, symbols: list[str]) -> list[SignalScore]:
        timeframe = self.config.strategy.get("bar_timeframe", "1Day")
        lookback = int(self.config.strategy.get("lookback_bars", 100))
        results: list[SignalScore] = []

        async def _scan_one(sym: str) -> None:
            try:
                df = await self.client.get_bars(sym, timeframe=timeframe, limit=lookback)
            except Exception as e:
                logger.warning(f"bars fetch {sym} failed: {e}")
                return
            if df is None or len(df) < 30:
                return
            sig = self.strategy.score(sym, df)
            results.append(sig)

        # Stagger to be polite to data API
        for i in range(0, len(symbols), 5):
            batch = symbols[i:i + 5]
            await asyncio.gather(*(_scan_one(s) for s in batch))
            await asyncio.sleep(0.2)
        return results

    # ── order execution ───────────────────────────────────────────────────────

    async def _execute_signal(self, sig: SignalScore, account: dict) -> None:
        live_price = await self.client.get_current_price(sig.symbol)
        params = self.risk.size_auto_order(sig, account, reference_price=live_price)
        if params is None:
            await self.repo.insert_signal(
                symbol=sig.symbol, score=sig.score, signals=sig.signals,
                rsi=sig.rsi, volume_ratio=sig.volume_ratio, price=sig.price,
                action_taken="skipped_sizing",
            )
            return

        client_order_id = f"auto_{sig.symbol}_{uuid.uuid4().hex[:10]}"
        reasoning = ", ".join(sig.signals)

        # Persist pending row BEFORE submitting (idempotency / crash recovery)
        await self.repo.insert_trade_pending(
            client_order_id=client_order_id,
            symbol=sig.symbol,
            side="buy",
            qty=params.qty,
            order_type="bracket",
            strategy=self.strategy.name,
            signals=sig.signals,
            signal_score=sig.score,
            reasoning=reasoning,
            stop_loss_price=params.stop_loss_price,
            take_profit_price=params.take_profit_price,
            trading_mode=self.risk.trading_mode(),
            source="auto",
        )

        try:
            result = await self.client.submit_market_order(
                symbol=sig.symbol,
                qty=params.qty,
                side="buy",
                client_order_id=client_order_id,
                stop_loss_price=params.stop_loss_price,
                take_profit_price=params.take_profit_price,
            )
            await self.repo.update_trade_after_submit(
                client_order_id=client_order_id,
                broker_order_id=result["id"],
                status=result["status"],
            )
            await self.repo.insert_signal(
                symbol=sig.symbol, score=sig.score, signals=sig.signals,
                rsi=sig.rsi, volume_ratio=sig.volume_ratio, price=sig.price,
                action_taken="order_placed",
            )
            await self.repo.log_event(
                "order_placed",
                f"{sig.symbol} buy {params.qty}@~${sig.price:.2f} score={sig.score} style={self.risk.trading_style()}",
            )
            logger.info(
                f"BUY {sig.symbol} x{params.qty} (score={sig.score}, "
                f"SL=${params.stop_loss_price}, TP=${params.take_profit_price})"
            )
        except Exception as e:
            logger.error(f"Order submission failed for {sig.symbol}: {e}")
            await self.repo.update_trade_after_submit(
                client_order_id=client_order_id,
                broker_order_id=None,
                status="error",
            )
            await self.repo.log_event("order_error", f"{sig.symbol}: {e}")

    # ── manual trade entrypoint (called by API route) ─────────────────────────

    async def submit_manual_trade(
        self, *, symbol: str, side: str, dollar_amount: float
    ) -> dict:
        if self.risk.kill_switch_active():
            return {"ok": False, "error": "kill switch active"}

        symbol = symbol.upper().strip()
        side = side.lower().strip()
        if side not in ("buy", "sell"):
            return {"ok": False, "error": f"invalid side: {side}"}

        account = await self.client.get_account()
        if account.get("trading_blocked"):
            return {"ok": False, "error": "account trading blocked"}

        # Daily loss limit applies to manual trades too
        await self.risk.initialize_daily_baseline(float(account["equity"]))
        if self.risk.daily_loss_breached(float(account["equity"])):
            return {"ok": False, "error": "daily loss limit breached"}

        if side == "sell":
            positions = await self.client.get_positions()
            held = {p["symbol"]: p for p in positions}
            if symbol not in held:
                return {"ok": False, "error": f"no position in {symbol} to sell"}
            client_order_id = f"man_{symbol}_{uuid.uuid4().hex[:10]}"
            qty = int(float(held[symbol]["qty"]))
            await self.repo.insert_trade_pending(
                client_order_id=client_order_id,
                symbol=symbol, side="sell", qty=qty, order_type="market",
                strategy="manual", trading_mode=self.risk.trading_mode(),
                source="manual",
            )
            try:
                result = await self.client.submit_market_order(
                    symbol=symbol, qty=qty, side="sell",
                    client_order_id=client_order_id,
                )
                await self.repo.update_trade_after_submit(
                    client_order_id=client_order_id,
                    broker_order_id=result["id"], status=result["status"],
                )
                await self.repo.log_event("manual_sell", f"{symbol} qty={qty}")
                return {"ok": True, "order": result}
            except Exception as e:
                await self.repo.update_trade_after_submit(
                    client_order_id=client_order_id, broker_order_id=None,
                    status="error",
                )
                return {"ok": False, "error": str(e)}

        # buy path
        price = await self.client.get_latest_price(symbol)
        if price is None or price <= 0:
            return {"ok": False, "error": f"could not fetch price for {symbol}"}

        params = self.risk.size_manual_order(
            price=price,
            dollar_amount=dollar_amount,
            buying_power=float(account["buying_power"]),
        )
        if params is None:
            return {
                "ok": False,
                "error": f"insufficient funds or amount too small (price=${price:.2f})",
            }

        client_order_id = f"man_{symbol}_{uuid.uuid4().hex[:10]}"
        await self.repo.insert_trade_pending(
            client_order_id=client_order_id,
            symbol=symbol, side="buy", qty=params.qty, order_type="bracket",
            strategy="manual",
            stop_loss_price=params.stop_loss_price,
            take_profit_price=params.take_profit_price,
            trading_mode=self.risk.trading_mode(), source="manual",
        )
        try:
            result = await self.client.submit_market_order(
                symbol=symbol, qty=params.qty, side="buy",
                client_order_id=client_order_id,
                stop_loss_price=params.stop_loss_price,
                take_profit_price=params.take_profit_price,
            )
            await self.repo.update_trade_after_submit(
                client_order_id=client_order_id,
                broker_order_id=result["id"], status=result["status"],
            )
            await self.repo.log_event(
                "manual_buy",
                f"{symbol} qty={params.qty} ~${params.notional:.2f}",
            )
            return {"ok": True, "order": result, "qty": params.qty,
                    "estimated_cost": params.notional}
        except Exception as e:
            await self.repo.update_trade_after_submit(
                client_order_id=client_order_id, broker_order_id=None, status="error",
            )
            return {"ok": False, "error": str(e)}

    # ── option order (manual, called by API route) ───────────────────────────

    async def submit_option_order(
        self,
        *,
        contract_symbol: str,
        underlying_symbol: str,
        contract_type: str,
        expiration_date: str,
        strike_price: float,
        side: str,
        qty: int,
    ) -> dict:
        if self.risk.kill_switch_active():
            return {"ok": False, "error": "kill switch active"}

        side = side.lower().strip()
        if side not in ("buy", "sell"):
            return {"ok": False, "error": f"invalid side: {side}"}

        account = await self.client.get_account()
        if account.get("trading_blocked"):
            return {"ok": False, "error": "account trading blocked"}

        await self.risk.initialize_daily_baseline(float(account["equity"]))
        if self.risk.daily_loss_breached(float(account["equity"])):
            return {"ok": False, "error": "daily loss limit breached"}

        # For sells, verify the contract is held
        if side == "sell":
            positions = await self.client.get_positions()
            held = {p["symbol"] for p in positions}
            if contract_symbol not in held:
                return {
                    "ok": False,
                    "error": f"no open position in {contract_symbol} to sell",
                }

        from datetime import date as _date
        try:
            exp_date = _date.fromisoformat(expiration_date)
        except (ValueError, TypeError):
            return {"ok": False, "error": f"invalid expiration_date: {expiration_date}"}

        client_order_id = f"opt_{underlying_symbol}_{uuid.uuid4().hex[:10]}"

        # Write pending row BEFORE submitting (crash-recovery invariant)
        await self.repo.insert_option_trade_pending(
            client_order_id=client_order_id,
            contract_symbol=contract_symbol,
            underlying_symbol=underlying_symbol,
            contract_type=contract_type,
            expiration_date=exp_date,
            strike_price=strike_price,
            side=side,
            qty=qty,
            trading_mode=self.risk.trading_mode(),
        )

        try:
            result = await self.client.submit_option_order(
                contract_symbol=contract_symbol,
                qty=qty,
                side=side,
                client_order_id=client_order_id,
            )
            await self.repo.update_option_trade_after_submit(
                client_order_id=client_order_id,
                broker_order_id=result["id"],
                status=result["status"],
            )
            await self.repo.log_event(
                "option_order_placed",
                f"{contract_symbol} {side} x{qty} (mode={self.risk.trading_mode()})",
            )
            logger.info(
                f"OPTION {side.upper()} {contract_symbol} x{qty} "
                f"(underlying={underlying_symbol} {contract_type} "
                f"strike={strike_price} exp={expiration_date})"
            )
            return {"ok": True, "order": result, "client_order_id": client_order_id}
        except Exception as e:
            logger.error(f"Option order submission failed for {contract_symbol}: {e}")
            await self.repo.update_option_trade_after_submit(
                client_order_id=client_order_id,
                broker_order_id=None,
                status="error",
            )
            await self.repo.log_event("option_order_error", f"{contract_symbol}: {e}")
            return {"ok": False, "error": str(e)}

    # ── snapshot for API ──────────────────────────────────────────────────────

    def snapshot_signals(self) -> list[dict]:
        return [
            {
                "symbol": s.symbol,
                "score": s.score,
                "signals": s.signals,
                "rsi": round(s.rsi, 2),
                "volume_ratio": round(s.volume_ratio, 2),
                "price": round(s.price, 2),
            }
            for s in self._last_scan_signals
        ]
