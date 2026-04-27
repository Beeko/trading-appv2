"""Streamlit dashboard for the trading engine. All data comes from the engine
HTTP API — UI holds no state of its own."""
import os
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

ENGINE_URL = os.getenv("ENGINE_URL", "http://localhost:8000")
TIMEOUT = 10


# ── api helpers ───────────────────────────────────────────────────────────────

def _get(path: str, **params):
    try:
        r = requests.get(f"{ENGINE_URL}{path}", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"GET {path}: {e}")
        return None


def _post(path: str, json: dict | None = None):
    try:
        r = requests.post(f"{ENGINE_URL}{path}", json=json, timeout=TIMEOUT)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            return {"ok": False, "error": detail}
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── page setup ────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Trading Engine",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Auto-refresh every 15 seconds
st_autorefresh(interval=15_000, key="autorefresh")

# ── sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚡ Trading Engine")

    status = _get("/engine/status") or {}
    mode = status.get("trading_mode", "unknown")
    if mode == "paper":
        st.info(f"Mode: **PAPER**")
    else:
        st.error(f"Mode: **LIVE**")

    if status.get("kill_switch"):
        st.error("🛑 KILL SWITCH ACTIVE")
    elif status.get("paused"):
        st.warning("⏸ Engine paused")
    elif status.get("running"):
        st.success("✅ Engine running")
    else:
        st.warning("⚠ Engine stopped")

    st.divider()

    # Kill switch
    st.subheader("Kill Switch")
    if status.get("kill_switch"):
        if st.button("RESUME TRADING", type="primary", use_container_width=True):
            _post("/resume")
            st.rerun()
    else:
        if st.button("🛑 EMERGENCY STOP", type="secondary", use_container_width=True):
            _post("/kill")
            st.rerun()

    # Engine pause/resume
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Pause", use_container_width=True, disabled=status.get("paused")):
            _post("/engine/pause")
            st.rerun()
    with col2:
        if st.button("Resume", use_container_width=True, disabled=not status.get("paused")):
            _post("/engine/resume")
            st.rerun()

    st.divider()

    # Manual trade
    st.subheader("Manual Trade")
    with st.form("manual_trade", clear_on_submit=False):
        m_symbol = st.text_input("Symbol", placeholder="TSLA").upper().strip()
        m_side = st.selectbox("Side", ["buy", "sell"])
        m_amount = st.number_input(
            "Amount ($) — ignored on sell",
            min_value=0.0, value=200.0, step=50.0,
        )
        submitted = st.form_submit_button("Submit", type="primary", use_container_width=True)
        if submitted:
            if not m_symbol:
                st.error("Symbol required")
            else:
                res = _post("/trade/manual", {
                    "symbol": m_symbol, "side": m_side, "amount": float(m_amount),
                })
                if res.get("ok"):
                    st.success(f"Order submitted! qty={res.get('qty', '?')}")
                else:
                    st.error(f"Failed: {res.get('error', 'unknown')}")

    st.divider()

    # Liquidate-all (gated)
    with st.expander("⚠ Liquidate All Positions"):
        st.caption("Closes every open position at market. Cannot be undone.")
        confirm_text = st.text_input(
            "Type LIQUIDATE_ALL to enable", key="liq_confirm"
        )
        if st.button("Liquidate Now", disabled=(confirm_text != "LIQUIDATE_ALL")):
            res = _post("/positions/liquidate-all", {"confirm": "LIQUIDATE_ALL"})
            if res.get("ok"):
                st.success("Liquidation submitted")
            else:
                st.error(res.get("error", "failed"))


# ── main: tabs ────────────────────────────────────────────────────────────────

st.title("📊 Trading Dashboard")

tabs = st.tabs(["Portfolio", "Live Signals", "Trade Log", "Events", "Config"])

# ── Portfolio tab ─────────────────────────────────────────────────────────────

with tabs[0]:
    account = _get("/account") or {}
    if account:
        c1, c2, c3, c4 = st.columns(4)
        equity = account.get("equity", 0)
        last_eq = account.get("last_equity", equity)
        day_change = equity - last_eq
        day_pct = (day_change / last_eq * 100) if last_eq else 0

        c1.metric("Equity", f"${equity:,.2f}",
                  f"${day_change:+,.2f} ({day_pct:+.2f}%)")
        c2.metric("Cash", f"${account.get('cash', 0):,.2f}")
        c3.metric("Buying Power", f"${account.get('buying_power', 0):,.2f}")
        c4.metric("Day Trades", f"{account.get('day_trade_count', 0)}")

        if account.get("trading_blocked"):
            st.error("🚫 Trading is blocked on this account")
        if account.get("pattern_day_trader"):
            st.warning("⚠ Pattern Day Trader flag is set on the account")

    st.subheader("Open Positions")
    positions = _get("/positions") or []
    if positions:
        df = pd.DataFrame(positions)
        df["unrealized_plpc"] = df["unrealized_plpc"].apply(lambda x: f"{(x or 0)*100:+.2f}%")
        for col in ("avg_entry_price", "current_price", "market_value", "unrealized_pl"):
            if col in df.columns:
                df[col] = df[col].apply(
                    lambda v: f"${v:,.2f}" if v is not None else "—"
                )
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No open positions")

    clock = _get("/clock") or {}
    if clock:
        if clock.get("is_open"):
            st.success(f"🟢 Market open — next close: {clock.get('next_close', '?')}")
        else:
            st.info(f"🔴 Market closed — next open: {clock.get('next_open', '?')}")


# ── Live Signals tab ──────────────────────────────────────────────────────────

with tabs[1]:
    st.subheader("Latest Scan Results")
    current = _get("/signals/current") or []
    if current:
        df = pd.DataFrame(current)
        df = df.sort_values("score", ascending=False)
        df["signals"] = df["signals"].apply(lambda s: ", ".join(s) if s else "")
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No scan completed yet (engine runs on a timer; refresh in a moment).")

    st.subheader("Recent Signal History (DB)")
    history = _get("/signals", limit=50) or []
    if history:
        df = pd.DataFrame(history)
        df["signals"] = df["signals"].apply(lambda s: ", ".join(s) if s else "")
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.caption("No history yet")


# ── Trade Log tab ─────────────────────────────────────────────────────────────

with tabs[2]:
    st.subheader("Trade History")
    trades = _get("/trades", limit=200) or []
    if trades:
        df = pd.DataFrame(trades)
        cols = [
            "created_at", "symbol", "side", "qty", "filled_qty",
            "filled_avg_price", "stop_loss_price", "take_profit_price",
            "status", "source", "strategy", "signal_score", "trading_mode",
        ]
        cols = [c for c in cols if c in df.columns]
        st.dataframe(df[cols], use_container_width=True, hide_index=True)
    else:
        st.info("No trades yet")

    st.subheader("Open Orders (Alpaca)")
    orders = _get("/orders", status="open", limit=50) or []
    if orders:
        df = pd.DataFrame(orders)
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.caption("No open orders")


# ── Events tab ────────────────────────────────────────────────────────────────

with tabs[3]:
    st.subheader("Engine Events")
    events = _get("/events", limit=100) or []
    if events:
        df = pd.DataFrame(events)
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No events logged yet")


# ── Config tab ────────────────────────────────────────────────────────────────

with tabs[4]:
    st.subheader("Active Configuration")
    cfg = _get("/config") or {}
    st.json(cfg)
    st.caption(
        "Edit `config.yaml` on the host and click Reload — no container restart needed."
    )
    if st.button("Reload config.yaml"):
        res = _post("/config/reload")
        if res.get("ok"):
            st.success("Config reloaded")
            st.rerun()
        else:
            st.error(res.get("error", "reload failed"))


st.caption(f"Auto-refreshing every 15s • Last update: {datetime.now().strftime('%H:%M:%S')}")
