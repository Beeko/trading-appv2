"""Hand-rolled technical indicators. No TA-Lib / pandas-ta dependency."""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class SignalScore:
    symbol: str = ""
    score: int = 0
    signals: list[str] = field(default_factory=list)
    rsi: float = 0.0
    volume_ratio: float = 0.0
    price: float = 0.0
    macd_bullish_cross: bool = False
    above_bb_upper: bool = False
    ema9_above_ema21: bool = False


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    fast_ema = ema(close, fast)
    slow_ema = ema(close, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def bollinger_bands(close: pd.Series, period: int = 20, std_dev: float = 2.0):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return upper, mid, lower


def calculate_signals(df: pd.DataFrame, symbol: str = "") -> SignalScore:
    """Score a symbol based on indicator confluence. Returns -5..+8 ish.

    Bullish indicators add to score, bearish subtract. The trading engine uses
    a configurable min_score_to_trade threshold.
    """
    if df is None or len(df) < 30:
        return SignalScore(symbol=symbol, score=-99, signals=["INSUFFICIENT_DATA"])

    close = df["close"].astype(float)
    volume = df["volume"].astype(float)

    macd_line, signal_line, _ = macd(close)
    rsi_vals = rsi(close)
    bb_upper, _, bb_lower = bollinger_bands(close)
    ema9 = ema(close, 9)
    ema21 = ema(close, 21)
    vol_ma20 = volume.rolling(20).mean()

    curr_close = float(close.iloc[-1])
    curr_macd = float(macd_line.iloc[-1])
    curr_signal = float(signal_line.iloc[-1])
    prev_macd = float(macd_line.iloc[-2])
    prev_signal = float(signal_line.iloc[-2])
    curr_rsi = float(rsi_vals.iloc[-1]) if not pd.isna(rsi_vals.iloc[-1]) else 50.0
    curr_bb_upper = float(bb_upper.iloc[-1])
    curr_bb_lower = float(bb_lower.iloc[-1])
    curr_ema9 = float(ema9.iloc[-1])
    curr_ema21 = float(ema21.iloc[-1])
    curr_vol = float(volume.iloc[-1])
    curr_vol_ma = float(vol_ma20.iloc[-1]) if not pd.isna(vol_ma20.iloc[-1]) else curr_vol
    vol_ratio = curr_vol / curr_vol_ma if curr_vol_ma > 0 else 1.0

    score = 0
    signals: list[str] = []

    # ── MACD ──────────────────────────────────────────────────────────────────
    macd_cross = (prev_macd <= prev_signal) and (curr_macd > curr_signal)
    if macd_cross:
        score += 2
        signals.append("MACD_BULLISH_CROSS")
    elif curr_macd > curr_signal and curr_macd > 0:
        score += 1
        signals.append("MACD_BULLISH")
    elif curr_macd < curr_signal:
        score -= 1
        signals.append("MACD_BEARISH")

    # ── RSI ───────────────────────────────────────────────────────────────────
    if 40 <= curr_rsi <= 65:
        score += 1
        signals.append(f"RSI_HEALTHY({curr_rsi:.1f})")
    elif curr_rsi < 30:
        score += 1  # potential bounce
        signals.append(f"RSI_OVERSOLD({curr_rsi:.1f})")
    elif curr_rsi > 75:
        score -= 2
        signals.append(f"RSI_OVERBOUGHT({curr_rsi:.1f})")
    elif curr_rsi > 65:
        signals.append(f"RSI_ELEVATED({curr_rsi:.1f})")

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    above_upper = curr_close > curr_bb_upper
    if above_upper and vol_ratio > 1.5:
        score += 2
        signals.append("BB_BREAKOUT_HIGH_VOL")
    elif above_upper:
        score += 1
        signals.append("BB_BREAKOUT")
    elif curr_close < curr_bb_lower and curr_rsi < 35:
        score += 1
        signals.append("BB_OVERSOLD_BOUNCE_SETUP")

    # ── EMA cross ─────────────────────────────────────────────────────────────
    ema_bullish = curr_ema9 > curr_ema21
    if ema_bullish:
        score += 1
        signals.append("EMA9_ABOVE_EMA21")
    else:
        score -= 1
        signals.append("EMA9_BELOW_EMA21")

    # ── Volume ────────────────────────────────────────────────────────────────
    if vol_ratio > 3.0:
        score += 2
        signals.append(f"VOLUME_SURGE({vol_ratio:.1f}x)")
    elif vol_ratio > 1.5:
        score += 1
        signals.append(f"VOLUME_ELEVATED({vol_ratio:.1f}x)")

    return SignalScore(
        symbol=symbol,
        score=score,
        signals=signals,
        rsi=curr_rsi,
        volume_ratio=vol_ratio,
        price=curr_close,
        macd_bullish_cross=macd_cross,
        above_bb_upper=above_upper,
        ema9_above_ema21=ema_bullish,
    )
