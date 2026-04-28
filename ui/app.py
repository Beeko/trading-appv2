"""Streamlit dashboard for the trading engine. All data comes from the engine
HTTP API — UI holds no state of its own."""
import os
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots
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

    # Trading style
    _STYLE_OPTIONS = ["conservative", "moderate", "aggressive"]
    _STYLE_INFO = {
        "conservative": "1% pos · SL 3% · TP 6% · Score ≥5 · 5 max positions",
        "moderate":     "2% pos · SL 5% · TP 10% · Score ≥3 · 10 max positions",
        "aggressive":   "4% pos · SL 7% · TP 20% · Score ≥2 · 15 max positions",
    }
    current_style = status.get("trading_style", "moderate")
    style_idx = _STYLE_OPTIONS.index(current_style) if current_style in _STYLE_OPTIONS else 1
    st.subheader("Trading Style")
    selected_style = st.radio(
        "style",
        _STYLE_OPTIONS,
        index=style_idx,
        horizontal=True,
        label_visibility="collapsed",
        key="style_radio",
    )
    st.caption(_STYLE_INFO.get(selected_style, ""))
    if selected_style != current_style:
        res = _post("/config/style", {"style": selected_style})
        if res.get("ok"):
            st.rerun()
        else:
            st.error(res.get("error", "style update failed"))

    # Daily goal compact progress
    _sb_goal = float(status.get("daily_profit_goal", 0))
    _sb_start = status.get("daily_start_equity")
    if _sb_goal > 0 and _sb_start:
        _sb_account = _get("/account") or {}
        _sb_equity = float(_sb_account.get("equity", _sb_start))
        _sb_gain = _sb_equity - float(_sb_start)
        _sb_pct = max(0.0, min(_sb_gain / _sb_goal, 1.0))
        st.caption(f"Daily goal: ${_sb_gain:+,.0f} / ${_sb_goal:,.0f}")
        st.progress(_sb_pct)

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

tabs = st.tabs(["Portfolio", "Live Signals", "Charts", "Trade Log", "Events", "Options", "Config"])

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

    # Daily profit goal progress
    _port_status = _get("/engine/status") or {}
    _goal = float(_port_status.get("daily_profit_goal", 0))
    _start_eq = _port_status.get("daily_start_equity")
    if _goal > 0 and _start_eq and account:
        _equity = float(account.get("equity", 0))
        _daily_gain = _equity - float(_start_eq)
        _progress = max(0.0, min(_daily_gain / _goal, 1.0))
        st.subheader("Daily Profit Goal")
        gc1, gc2, gc3 = st.columns(3)
        gc1.metric("Today's Gain", f"${_daily_gain:+,.2f}")
        gc2.metric("Goal", f"${_goal:,.2f}")
        gc3.metric("Remaining", f"${max(0.0, _goal - _daily_gain):,.2f}")
        _bar_label = f"${_daily_gain:+,.2f} / ${_goal:,.2f}  ({_progress*100:.0f}%)"
        st.progress(_progress, text=_bar_label)
        if _daily_gain >= _goal:
            st.success("Goal reached — engine will pause on next tick to lock in profit.")
        elif _progress >= 0.6:
            st.info(f"Over 60% there — engine is accepting slightly lower-scored signals to push toward goal.")

    with st.expander("Set Daily Profit Goal"):
        _goal_cfg = _get("/config") or {}
        _cur_goal = float(_goal_cfg.get("risk", {}).get("daily_profit_goal", 0))
        with st.form("goal_form"):
            _new_goal = st.number_input(
                "Daily target ($)", min_value=0.0, value=_cur_goal, step=50.0,
            )
            st.caption("Set to 0 to disable. Engine pauses once daily gain hits this amount.")
            if st.form_submit_button("Update Goal", type="primary"):
                _res = _post("/config/daily-goal", {"goal": float(_new_goal)})
                if _res.get("ok"):
                    st.success(f"Goal updated to ${_new_goal:,.2f}" if _new_goal > 0 else "Goal disabled")
                    st.rerun()
                else:
                    st.error(_res.get("error", "update failed"))

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
    cfg = _get("/config") or {}
    screener_cfg = cfg.get("screener", {})
    wsb_cfg = cfg.get("wsb_scanner", {})

    if screener_cfg.get("enabled"):
        st.info(
            f"Watchlist source: **Alpaca Screener** "
            f"(top {screener_cfg.get('top_n', 50)}, "
            f"${screener_cfg.get('min_price', 5)}–${screener_cfg.get('max_price', 500)})"
        )
        with st.expander("Preview screener symbols"):
            screener_data = _get("/screener/symbols") or {}
            syms = screener_data.get("symbols", [])
            if syms:
                cols = st.columns(5)
                for i, sym in enumerate(syms):
                    cols[i % 5].code(sym)
            else:
                st.caption("No symbols returned yet")
    elif wsb_cfg.get("enabled"):
        st.info("Watchlist source: **WSB Scanner** (Reddit trending)")
    else:
        watchlist = cfg.get("trading", {}).get("watchlist", [])
        st.info(f"Watchlist source: **Static list** ({len(watchlist)} symbols)")

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


# ── Charts tab ────────────────────────────────────────────────────────────────

with tabs[2]:
    if "chart_data" not in st.session_state:
        st.session_state.chart_data = None
    if "chart_symbol" not in st.session_state:
        st.session_state.chart_symbol = ""

    # Quick-pick from last scan
    current_for_chart = _get("/signals/current") or []
    top_sigs = sorted(current_for_chart, key=lambda s: s.get("score", 0), reverse=True)[:8]
    if top_sigs:
        st.caption("Quick-pick from last scan (sorted by score):")
        qcols = st.columns(len(top_sigs))
        for i, sig in enumerate(top_sigs):
            label = f"{sig['symbol']} ({sig.get('score', 0):+d})"
            if qcols[i].button(label, key=f"qs_{sig['symbol']}"):
                st.session_state.chart_symbol = sig["symbol"]
                st.session_state.chart_data = None
                st.rerun()

    c1, c2, c3 = st.columns([3, 2, 2])
    with c1:
        sym_input = st.text_input(
            "Symbol", value=st.session_state.chart_symbol, key="chart_sym_input"
        )
    with c2:
        tf_input = st.selectbox(
            "Timeframe", ["1Day", "1Hour", "15Min", "5Min"], key="chart_tf"
        )
    with c3:
        limit_input = st.selectbox("Bars", [50, 100, 200], index=1, key="chart_limit")

    if st.button("View Chart", type="primary"):
        sym = sym_input.upper().strip() if sym_input else ""
        if not sym:
            st.warning("Enter a symbol first")
        else:
            st.session_state.chart_symbol = sym
            data = _get(f"/indicators/{sym}", timeframe=tf_input, limit=limit_input)
            if data and "ohlcv" in data:
                st.session_state.chart_data = data
            else:
                st.error(f"No indicator data returned for {sym}")
                st.session_state.chart_data = None

    chart = st.session_state.chart_data
    if chart:
        ts = chart["timestamps"]
        ohlcv = chart["ohlcv"]
        ind = chart["indicators"]
        sig = chart["signal"]

        score = sig["score"]
        score_color = "green" if score >= 3 else ("orange" if score >= 0 else "red")
        st.markdown(
            f"**{chart['symbol']}** &nbsp;|&nbsp; "
            f"Score: <span style='color:{score_color}'>{score:+d}</span> &nbsp;|&nbsp; "
            f"RSI: {sig['rsi']:.1f} &nbsp;|&nbsp; "
            f"Vol ratio: {sig['volume_ratio']:.1f}x &nbsp;|&nbsp; "
            f"Price: ${sig['price']:,.2f}",
            unsafe_allow_html=True,
        )
        if sig.get("signals"):
            st.caption("Signals: " + " · ".join(sig["signals"]))

        fig = make_subplots(
            rows=3, cols=1,
            shared_xaxes=True,
            row_heights=[0.60, 0.25, 0.15],
            vertical_spacing=0.03,
            subplot_titles=("Price  ·  Bollinger Bands  ·  EMA 9/21", "MACD", "RSI"),
        )

        # Price candles
        fig.add_trace(go.Candlestick(
            x=ts,
            open=ohlcv["open"], high=ohlcv["high"],
            low=ohlcv["low"], close=ohlcv["close"],
            name="Price",
            increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
        ), row=1, col=1)

        # Bollinger Bands (filled region)
        fig.add_trace(go.Scatter(
            x=ts, y=ind["bb_upper"],
            line=dict(color="rgba(100,100,220,0.5)", width=1),
            name="BB Upper", showlegend=False,
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=ts, y=ind["bb_lower"],
            line=dict(color="rgba(100,100,220,0.5)", width=1),
            fill="tonexty", fillcolor="rgba(100,100,220,0.07)",
            name="BB Lower", showlegend=False,
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=ts, y=ind["bb_mid"],
            line=dict(color="rgba(100,100,220,0.5)", width=1, dash="dot"),
            name="BB Mid", showlegend=False,
        ), row=1, col=1)

        # EMA lines
        fig.add_trace(go.Scatter(
            x=ts, y=ind["ema9"],
            line=dict(color="#ffa500", width=1.5),
            name="EMA 9",
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=ts, y=ind["ema21"],
            line=dict(color="#00bcd4", width=1.5),
            name="EMA 21",
        ), row=1, col=1)

        # MACD histogram (green above zero, red below)
        hist_colors = ["#26a69a" if (v or 0) >= 0 else "#ef5350" for v in ind["macd_hist"]]
        fig.add_trace(go.Bar(
            x=ts, y=ind["macd_hist"],
            marker_color=hist_colors,
            name="Histogram", showlegend=False,
        ), row=2, col=1)
        fig.add_trace(go.Scatter(
            x=ts, y=ind["macd_line"],
            line=dict(color="#2196f3", width=1.5),
            name="MACD",
        ), row=2, col=1)
        fig.add_trace(go.Scatter(
            x=ts, y=ind["macd_signal"],
            line=dict(color="#ff9800", width=1.5),
            name="Signal",
        ), row=2, col=1)

        # RSI + reference lines at 70 / 30
        fig.add_trace(go.Scatter(
            x=ts, y=ind["rsi"],
            line=dict(color="#9c27b0", width=1.5),
            name="RSI",
        ), row=3, col=1)
        valid_ts = [t for t, v in zip(ts, ind["rsi"]) if v is not None]
        if valid_ts:
            for level, color in [(70, "#ef5350"), (30, "#26a69a")]:
                fig.add_trace(go.Scatter(
                    x=[valid_ts[0], valid_ts[-1]], y=[level, level],
                    line=dict(color=color, width=1, dash="dash"),
                    showlegend=False,
                ), row=3, col=1)

        fig.update_layout(
            height=720,
            template="plotly_dark",
            margin=dict(l=10, r=10, t=40, b=10),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        fig.update_xaxes(rangeslider_visible=False)
        fig.update_yaxes(title_text="Price ($)", row=1, col=1)
        fig.update_yaxes(title_text="MACD", row=2, col=1)
        fig.update_yaxes(title_text="RSI", row=3, col=1, range=[0, 100])

        st.plotly_chart(fig, use_container_width=True)


# ── Trade Log tab ─────────────────────────────────────────────────────────────

with tabs[3]:
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

with tabs[4]:
    st.subheader("Engine Events")
    events = _get("/events", limit=100) or []
    if events:
        df = pd.DataFrame(events)
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No events logged yet")


# ── Options helpers ───────────────────────────────────────────────────────────

def _render_options_engine_panel():
    status = _get("/options/engine/status") or {}
    cols = st.columns([1, 2, 1, 1])
    with cols[0]:
        if status.get("paused"):
            st.warning("Options: ⏸ paused")
        elif status.get("running"):
            st.success("Options: ✅ running")
        else:
            st.error("Options: ⚠ stopped")
    with cols[1]:
        last_scan = status.get("last_scan_at") or "—"
        st.caption(f"last scan: {last_scan}")
    with cols[2]:
        if st.button("Pause options", key="opt_pause", disabled=status.get("paused", False)):
            _post("/options/engine/pause")
            st.rerun()
    with cols[3]:
        if st.button("Resume options", key="opt_resume", disabled=not status.get("paused", False)):
            _post("/options/engine/resume")
            st.rerun()


def _render_open_options_positions():
    data = _get("/options/positions")
    if not data or data.get("count", 0) == 0:
        st.info("No open option positions.")
        return

    rows = []
    for p in data["positions"]:
        rows.append({
            "Contract": p["contract_symbol"],
            "Side": p["side"],
            "Qty": p["qty"],
            "Entry mid": p.get("entry_mid"),
            "Current mid": p.get("current_mid"),
            "P&L %": (p["pnl_pct"] * 100) if p.get("pnl_pct") is not None else None,
            "Entry Δ": p.get("entry_delta"),
            "Current Δ": p.get("current_delta"),
            "IV": p.get("current_iv"),
            "DTE": p.get("dte_remaining"),
            "Next trigger": p.get("next_trigger") or "—",
        })
    df = pd.DataFrame(rows)

    def _color_pnl(v):
        if pd.isna(v):
            return ""
        return "color: green" if v > 0 else "color: red" if v < 0 else ""

    def _color_trigger(v):
        return "color: orange; font-weight: bold" if v and v != "—" else ""

    styled = (
        df.style
        .map(_color_pnl, subset=["P&L %"])
        .map(_color_trigger, subset=["Next trigger"])
        .format({
            "Entry mid": "${:.2f}", "Current mid": "${:.2f}", "P&L %": "{:+.1f}%",
            "Entry Δ": "{:.2f}", "Current Δ": "{:.2f}", "IV": "{:.1%}",
        }, na_rep="—")
    )
    st.dataframe(styled, use_container_width=True)


def _render_options_chain_explorer():
    symbol = st.text_input("Symbol", value="AAPL", key="opt_chain_sym").upper()
    days_out = st.slider("Days out", 7, 90, 45, key="opt_chain_days")
    cfilter = st.selectbox("Type", ["any", "call", "put"], key="opt_chain_type")
    hide_illiquid = st.checkbox(
        "Hide illiquid (engine filter)", value=True, key="opt_chain_liq",
    )

    if not symbol:
        return

    type_param = "" if cfilter == "any" else cfilter
    data = _get(f"/options/chain/{symbol}", days_out=days_out, type=type_param)
    if not data or not data.get("contracts"):
        st.info("No contracts returned.")
        return

    cfg = _get("/config") or {}
    liq = ((cfg.get("options") or {}).get("liquidity") or {})
    max_spread = float(liq.get("max_spread_pct", 0.20))
    min_vol = int(liq.get("min_volume", 10))
    min_oi = int(liq.get("min_open_interest", 100))

    rows = []
    for c in data["contracts"]:
        spread = c.get("spread_pct")
        vol = c.get("volume") or 0
        oi = c.get("open_interest") or 0
        is_liquid = (
            spread is not None and spread <= max_spread
            and vol >= min_vol and oi >= min_oi
        )
        if hide_illiquid and not is_liquid:
            continue
        rows.append({
            "Symbol": c["symbol"],
            "Type": c["contract_type"],
            "Expiry": c["expiration_date"],
            "Strike": c["strike_price"],
            "Bid": c.get("bid"), "Ask": c.get("ask"), "Mid": c.get("mid"),
            "Spread%": (spread * 100) if spread is not None else None,
            "Δ": c.get("delta"), "Γ": c.get("gamma"),
            "Θ": c.get("theta"), "Vega": c.get("vega"),
            "IV": c.get("iv"),
            "Vol": vol, "OI": oi,
        })

    if not rows:
        st.info("No liquid contracts match. Toggle off 'Hide illiquid' to see all.")
        return

    df = pd.DataFrame(rows)
    st.dataframe(
        df.style.format({
            "Strike": "${:.2f}", "Bid": "${:.2f}", "Ask": "${:.2f}", "Mid": "${:.2f}",
            "Spread%": "{:.1f}%", "Δ": "{:.2f}", "Γ": "{:.3f}",
            "Θ": "{:.3f}", "Vega": "{:.3f}", "IV": "{:.1%}",
        }, na_rep="—"),
        use_container_width=True,
    )

    # ── Order form ────────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Place Option Order")
    if not rows:
        return
    contract_options = [r["Symbol"] for r in rows]
    contract_map_full = {c["symbol"]: c for c in data["contracts"]}

    with st.form("options_order_new", clear_on_submit=False):
        selected_sym = st.selectbox("Contract", contract_options, key="opt_contract_sel2")
        fc1, fc2 = st.columns(2)
        with fc1:
            qty = st.number_input("Qty (contracts)", min_value=1, max_value=50, value=1, step=1)
        with fc2:
            side = st.selectbox("Side", ["buy", "sell"], key="opt_side2")

        selected_c = contract_map_full.get(selected_sym, {})
        mid_price = selected_c.get("mid") or selected_c.get("close_price")
        if mid_price:
            total_cost = mid_price * 100 * qty
            label = "Estimated cost" if side == "buy" else "Estimated proceeds"
            st.info(f"{label}: ${mid_price:.2f} × 100 × {int(qty)} = **${total_cost:,.2f}**")

        submitted = st.form_submit_button("Submit Option Order", type="primary", use_container_width=True)
        if submitted and selected_c:
            payload = {
                "contract_symbol": selected_c["symbol"],
                "underlying_symbol": selected_c.get("underlying_symbol", symbol),
                "contract_type": selected_c.get("contract_type", ""),
                "expiration_date": selected_c["expiration_date"],
                "strike_price": selected_c.get("strike_price", 0),
                "side": side,
                "qty": int(qty),
            }
            res = _post("/options/order", json=payload)
            if res and res.get("ok"):
                st.success(f"Order submitted! {selected_sym} × {int(qty)}")
            else:
                st.error(f"Failed: {(res or {}).get('error', 'unknown error')}")


def _render_iv_surface():
    symbol = st.text_input("Symbol", value="AAPL", key="opt_iv_sym").upper()
    if not symbol:
        return

    data = _get(f"/options/iv-surface/{symbol}")
    if not data or not data.get("expirations"):
        st.info("No IV data available.")
        return

    fig = go.Figure()
    for exp in data["expirations"]:
        points = exp["points"]
        if not points:
            continue
        fig.add_trace(go.Scatter(
            x=[p["strike"] for p in points],
            y=[p["iv"] * 100 for p in points],
            mode="lines+markers",
            name=exp["expiration_date"],
        ))
    fig.update_layout(
        title=f"{symbol} IV by Strike",
        xaxis_title="Strike",
        yaxis_title="Implied Volatility (%)",
        height=480,
    )
    st.plotly_chart(fig, use_container_width=True)


# ── Options tab ───────────────────────────────────────────────────────────────

with tabs[5]:
    _render_options_engine_panel()
    st.divider()

    sub_view = st.radio(
        "View",
        options=["Open Positions", "Chain Explorer", "IV Surface", "Recent Trades"],
        horizontal=True,
        key="opt_subview",
    )

    if sub_view == "Open Positions":
        _render_open_options_positions()
    elif sub_view == "Chain Explorer":
        _render_options_chain_explorer()
    elif sub_view == "IV Surface":
        _render_iv_surface()
    else:
        # Recent Trades (keep existing functionality)
        opt_trades = _get("/options/trades", limit=50) or []
        if opt_trades:
            df = pd.DataFrame(opt_trades)
            cols = [
                "created_at", "underlying_symbol", "contract_type", "strike_price",
                "expiration_date", "side", "qty", "filled_avg_price", "status",
                "contract_symbol", "trading_mode",
            ]
            cols = [c for c in cols if c in df.columns]
            st.dataframe(df[cols], use_container_width=True, hide_index=True)
        else:
            st.info("No option trades yet")


# ── Config tab ────────────────────────────────────────────────────────────────

with tabs[6]:
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
