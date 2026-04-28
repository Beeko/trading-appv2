"""Contract selector pipeline. Filters chain by DTE → snapshot → liquidity →
delta, then picks the contract whose delta is closest to target.

Never relaxes filters to force a trade. Returns None on empty result."""
from dataclasses import dataclass
from datetime import date
from typing import Literal, Optional

from loguru import logger


Direction = Literal["bullish", "bearish"]


@dataclass
class SelectorConfig:
    target_delta: float
    delta_tolerance: float
    min_dte: int
    max_dte: int
    max_spread_pct: float
    min_volume: int
    min_open_interest: int
    dte_floor: int


@dataclass
class SelectedContract:
    contract_symbol: str
    underlying_symbol: str
    contract_type: str
    expiration_date: date
    strike_price: float
    delta: float
    gamma: float
    theta: float
    vega: float
    iv: float
    bid: float
    ask: float
    mid: float
    spread_pct: float
    volume: int
    open_interest: int
    dte: int


class ContractSelector:
    def __init__(self, alpaca_client, cfg: SelectorConfig):
        self.client = alpaca_client
        self.cfg = cfg

    async def select(
        self, underlying: str, direction: Direction
    ) -> Optional[SelectedContract]:
        contract_type = "call" if direction == "bullish" else "put"

        chain = await self.client.get_option_chain(
            underlying=underlying,
            days_out=self.cfg.max_dte,
            contract_type=contract_type,
        )
        if not chain:
            logger.info(f"selector[{underlying}]: empty chain")
            return None

        today = date.today()

        dte_filtered: list[tuple[dict, int]] = []
        for c in chain:
            exp_str = c.get("expiration_date")
            if not exp_str:
                continue
            try:
                exp = date.fromisoformat(exp_str)
            except ValueError:
                continue
            dte = (exp - today).days
            if dte <= self.cfg.dte_floor:
                continue
            if dte < self.cfg.min_dte or dte > self.cfg.max_dte:
                continue
            dte_filtered.append((c, dte))

        if not dte_filtered:
            logger.info(f"selector[{underlying}]: no contracts in DTE window")
            return None

        symbols = [c["symbol"] for c, _ in dte_filtered]

        snapshots = await self.client.get_option_snapshots(symbols)
        if not snapshots:
            logger.info(f"selector[{underlying}]: snapshot batch empty")
            return None

        candidates: list[tuple[SelectedContract, float]] = []
        target = self.cfg.target_delta if direction == "bullish" else -self.cfg.target_delta

        for contract, dte in dte_filtered:
            sym = contract["symbol"]
            snap = snapshots.get(sym)
            if not snap:
                continue
            if not self._passes_liquidity(snap):
                continue
            delta = snap.get("delta")
            if delta is None:
                continue
            if abs(delta - target) > self.cfg.delta_tolerance:
                continue

            try:
                exp = date.fromisoformat(contract["expiration_date"])
            except (ValueError, TypeError, KeyError):
                continue

            sc = SelectedContract(
                contract_symbol=sym,
                underlying_symbol=contract.get("underlying_symbol", underlying),
                contract_type=contract["contract_type"],
                expiration_date=exp,
                strike_price=float(contract["strike_price"]),
                delta=float(delta),
                gamma=float(snap.get("gamma") or 0),
                theta=float(snap.get("theta") or 0),
                vega=float(snap.get("vega") or 0),
                iv=float(snap.get("implied_volatility") or 0),
                bid=float(snap.get("bid") or 0),
                ask=float(snap.get("ask") or 0),
                mid=float(snap.get("mid") or 0),
                spread_pct=float(snap.get("spread_pct") or 0),
                volume=int(snap.get("volume") or 0),
                open_interest=int(snap.get("open_interest") or 0),
                dte=dte,
            )
            distance = abs(delta - target)
            candidates.append((sc, distance))

        if not candidates:
            logger.info(f"selector[{underlying}]: no candidates after liquidity+delta filters")
            return None

        candidates.sort(key=lambda pair: (pair[1], -pair[0].open_interest))
        return candidates[0][0]

    def _passes_liquidity(self, snap: dict) -> bool:
        bid = snap.get("bid") or 0
        ask = snap.get("ask") or 0
        mid = snap.get("mid") or 0
        if bid <= 0 or ask <= 0 or mid <= 0:
            return False
        spread_pct = snap.get("spread_pct")
        if spread_pct is None or spread_pct > self.cfg.max_spread_pct:
            return False
        if int(snap.get("volume") or 0) < self.cfg.min_volume:
            return False
        if int(snap.get("open_interest") or 0) < self.cfg.min_open_interest:
            return False
        return True
