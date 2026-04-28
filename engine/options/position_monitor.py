"""Position monitor for autonomous options. Evaluates exit triggers and
fires sell-to-close limit orders."""
import asyncio
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Optional

from loguru import logger


@dataclass
class MonitorConfig:
    profit_target_pct: float
    stop_loss_pct: float
    dte_floor: int
    fill_timeout_seconds: int = 120
    exit_retry_max: int = 3
    exit_retry_price_step_pct: float = 0.05


@dataclass
class ExitDecision:
    reason: str
    pnl_pct: float
    current_mid: float
    dte: int


def evaluate_exit(
    *, entry_mid: float, current_mid: float, dte: int, cfg: MonitorConfig
) -> Optional[ExitDecision]:
    """Evaluate exit triggers in priority order. Returns None if no trigger fires.

    Priority: dte_floor > profit_target > stop_loss.
    """
    if entry_mid <= 0:
        return None

    if dte < cfg.dte_floor:
        pnl = (current_mid - entry_mid) / entry_mid
        return ExitDecision(reason="dte_floor", pnl_pct=pnl,
                            current_mid=current_mid, dte=dte)

    pnl = (current_mid - entry_mid) / entry_mid

    if pnl >= cfg.profit_target_pct:
        return ExitDecision(reason="profit_target", pnl_pct=pnl,
                            current_mid=current_mid, dte=dte)

    if pnl <= -cfg.stop_loss_pct:
        return ExitDecision(reason="stop_loss", pnl_pct=pnl,
                            current_mid=current_mid, dte=dte)

    return None


def compute_dte(expiration_date: date, today: Optional[date] = None) -> int:
    today = today or date.today()
    return (expiration_date - today).days


class PositionMonitor:
    """Watches open option positions, fires sell-to-close on exit triggers."""

    def __init__(self, alpaca_client, repo, cfg: MonitorConfig):
        self.client = alpaca_client
        self.repo = repo
        self.cfg = cfg
        self._stop_event = asyncio.Event()

    async def run(self, interval_seconds: int) -> None:
        await self.startup_sweep()
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception as e:
                logger.exception(f"Position monitor tick failed: {e}")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop_event.set()

    async def startup_sweep(self) -> None:
        """On engine start, force-close any positions below DTE floor."""
        await self.tick(force_dte_check_only=True)

    async def tick(self, force_dte_check_only: bool = False) -> None:
        open_trades = await self.repo.list_open_option_trades()
        if not open_trades:
            return

        symbols = [t["contract_symbol"] for t in open_trades]
        snapshots = await self.client.get_option_snapshots(symbols)

        today = date.today()
        for trade in open_trades:
            sym = trade["contract_symbol"]
            snap = snapshots.get(sym)
            if not snap:
                logger.warning(f"monitor: no snapshot for {sym}; skipping")
                continue

            try:
                exp = date.fromisoformat(trade["expiration_date"])
            except (ValueError, TypeError, KeyError):
                logger.warning(f"monitor: bad expiration on {sym}")
                continue
            dte = (exp - today).days

            entry_mid = float(trade.get("entry_mid") or 0)
            current_mid = float(snap.get("mid") or 0)

            decision = evaluate_exit(
                entry_mid=entry_mid,
                current_mid=current_mid,
                dte=dte,
                cfg=self.cfg,
            )

            if force_dte_check_only and (decision is None or decision.reason != "dte_floor"):
                continue
            if decision is None:
                continue

            await self._submit_exit(trade, decision)

    async def _submit_exit(self, trade: dict, decision: ExitDecision) -> None:
        sym = trade["contract_symbol"]
        qty = int(trade["qty"])
        client_order_id = f"opt_exit_{trade['underlying_symbol']}_{uuid.uuid4().hex[:8]}"

        await self.repo.update_option_trade_exit(
            client_order_id=trade["client_order_id"],
            exit_mid=decision.current_mid,
            exit_dte=decision.dte,
            exit_reason=decision.reason,
            status="closing",
        )

        try:
            await self.client.submit_option_limit_order(
                contract_symbol=sym,
                qty=qty,
                side="sell",
                limit_price=decision.current_mid,
                client_order_id=client_order_id,
            )
            await self.repo.log_event(
                "option_exit_submitted",
                f"{sym} reason={decision.reason} mid=${decision.current_mid:.2f} "
                f"pnl={decision.pnl_pct*100:.1f}% dte={decision.dte}",
            )
            logger.info(
                f"OPTION EXIT {sym} reason={decision.reason} "
                f"pnl={decision.pnl_pct*100:.1f}% dte={decision.dte}"
            )
        except Exception as e:
            logger.error(f"Exit order failed for {sym}: {e}")
            await self.repo.log_event("option_exit_error", f"{sym}: {e}")
