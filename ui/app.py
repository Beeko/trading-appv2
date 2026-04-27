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


# ── Options tab ───────────────────────────────────────────────────────────────

with tabs[5]:
    if "option_chain" not in st.session_state:
        st.session_state.option_chain = []
    if "option_underlying" not in st.session_state:
        st.session_state.option_underlying = ""

    # ── Chain loader ──────────────────────────────────────────────────────────
    st.subheader("Options Chain")
    lc1, lc2 = st.columns([3, 2])
    with lc1:
        und_input = st.text_input(
            "Underlying Symbol",
            value=st.session_state.option_underlying,
            key="opt_sym",
            placeholder="AAPL",
        )
    with lc2:
        days_input = st.selectbox(
            "Expiry within", [7, 14, 21, 30, 45, 60, 90], index=4, key="opt_days"
        )

    if st.button("Load Chain", type="primary"):
        sym = und_input.upper().strip()
        if not sym:
            st.warning("Enter a symbol first")
        else:
            with st.spinner(f"Fetching option chain for {sym}…"):
                data = _get(f"/options/chain/{sym}", days_out=days_input)
            if data and isinstance(data.get("contracts"), list):
                st.session_state.option_chain = data["contracts"]
                st.session_state.option_underlying = sym
                if not data["contracts"]:
                    st.warning(
                        f"No contracts returned for {sym}. "
                        "Options trading may not be enabled on your Alpaca account, "
                        "or no contracts exist in this expiry window."
                    )
            else:
                st.error(f"Failed to load chain for {sym}")

    # ── Chain display ─────────────────────────────────────────────────────────
    chain = st.session_state.option_chain
    if chain:
        st.caption(
            f"{len(chain)} contracts loaded for "
            f"**{st.session_state.option_underlying}**"
        )

        expirations = sorted({c["expiration_date"] for c in chain if c["expiration_date"]})
        selected_exp = st.selectbox("Expiration date", expirations, key="opt_expiry")

        exp_contracts = [c for c in chain if c["expiration_date"] == selected_exp]
        calls = sorted(
            [c for c in exp_contracts if c.get("contract_type") == "call"],
            key=lambda x: x.get("strike_price") or 0,
        )
        puts = sorted(
            [c for c in exp_contracts if c.get("contract_type") == "put"],
            key=lambda x: x.get("strike_price") or 0,
        )

        def _chain_rows(contracts):
            return [
                {
                    "Strike": f"${c['strike_price']:.2f}" if c.get("strike_price") else "—",
                    "Last": f"${c['close_price']:.2f}" if c.get("close_price") else "—",
                    "OI": f"{c['open_interest']:,}" if c.get("open_interest") else "—",
                    "Symbol": c["symbol"],
                }
                for c in contracts
            ]

        ch1, ch2 = st.columns(2)
        with ch1:
            st.markdown("**Calls**")
            if calls:
                st.dataframe(
                    pd.DataFrame(_chain_rows(calls)).drop(columns=["Symbol"]),
                    use_container_width=True, hide_index=True,
                )
            else:
                st.caption("No call contracts")
        with ch2:
            st.markdown("**Puts**")
            if puts:
                st.dataframe(
                    pd.DataFrame(_chain_rows(puts)).drop(columns=["Symbol"]),
                    use_container_width=True, hide_index=True,
                )
            else:
                st.caption("No put contracts")

        # ── Order form ────────────────────────────────────────────────────────
        st.divider()
        st.subheader("Place Option Order")

        tradable = [c for c in exp_contracts if c.get("tradable", True)]
        if not tradable:
            st.warning("No tradable contracts for this expiration")
        else:
            def _contract_label(c):
                last = f" · last ${c['close_price']:.2f}" if c.get("close_price") else ""
                oi = f" · OI {c['open_interest']:,}" if c.get("open_interest") else ""
                return (
                    f"{c.get('contract_type','?').upper()} "
                    f"${c.get('strike_price', 0):.2f}"
                    f"{last}{oi}"
                )

            contract_map = {_contract_label(c): c for c in tradable}

            with st.form("options_order", clear_on_submit=False):
                selected_label = st.selectbox(
                    "Contract", list(contract_map.keys()), key="opt_contract_sel"
                )
                fc1, fc2 = st.columns(2)
                with fc1:
                    qty = st.number_input(
                        "Qty (contracts)", min_value=1, max_value=50, value=1, step=1
                    )
                with fc2:
                    side = st.selectbox("Side", ["buy", "sell"], key="opt_side")

                # Cost preview — prominent so the ×100 multiplier is unmissable
                selected_c = contract_map[selected_label]
                if selected_c.get("close_price"):
                    premium = selected_c["close_price"]
                    total_cost = premium * 100 * qty
                    if side == "buy":
                        st.info(
                            f"Estimated cost: ${premium:.2f} premium "
                            f"× 100 multiplier × {int(qty)} contract(s) "
                            f"= **${total_cost:,.2f}**"
                        )
                    else:
                        st.info(
                            f"Estimated proceeds: ${premium:.2f} premium "
                            f"× 100 × {int(qty)} = **${total_cost:,.2f}**"
                        )

                submitted = st.form_submit_button(
                    "Submit Option Order", type="primary", use_container_width=True
                )
                if submitted:
                    c = selected_c
                    payload = {
                        "contract_symbol": c["symbol"],
                        "underlying_symbol": c["underlying_symbol"],
                        "contract_type": c.get("contract_type", ""),
                        "expiration_date": c["expiration_date"],
                        "strike_price": c.get("strike_price", 0),
                        "side": side,
                        "qty": int(qty),
                    }
                    res = _post("/options/order", json=payload)
                    if res.get("ok"):
                        st.success(
                            f"Order submitted! "
                            f"{c.get('contract_type','').upper()} "
                            f"${c.get('strike_price'):.2f} "
                            f"exp {c['expiration_date']} × {int(qty)}"
                        )
                    else:
                        st.error(f"Failed: {res.get('error', 'unknown error')}")

    # ── Recent option trades ──────────────────────────────────────────────────
    st.divider()
    st.subheader("Recent Option Trades")
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
