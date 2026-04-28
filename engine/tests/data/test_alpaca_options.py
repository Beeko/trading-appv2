"""Tests for AlpacaClient option-snapshot normalization and limit orders."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from data.alpaca_client import AlpacaClient


def _fake_snapshot(*, delta=0.40, gamma=0.05, theta=-0.08, vega=0.15,
                   iv=0.35, bid=2.50, ask=2.60, volume=250, oi=1500):
    return SimpleNamespace(
        greeks=SimpleNamespace(delta=delta, gamma=gamma, theta=theta, vega=vega),
        implied_volatility=iv,
        latest_quote=SimpleNamespace(
            bid_price=bid, ask_price=ask, bid_size=10, ask_size=10
        ),
        latest_trade=SimpleNamespace(price=(bid + ask) / 2, size=volume),
        daily_bar=SimpleNamespace(volume=volume),
        open_interest=oi,
    )


@pytest.fixture
def client():
    return AlpacaClient(api_key="k", secret_key="s", paper=True)


async def test_get_option_snapshot_normalizes_greek_fields(client):
    fake_resp = {"AAPL260619C00200000": _fake_snapshot()}
    client.option_data = MagicMock()
    client.option_data.get_option_snapshot = MagicMock(return_value=fake_resp)

    snap = await client.get_option_snapshot("AAPL260619C00200000")

    assert snap is not None
    assert snap["delta"] == 0.40
    assert snap["gamma"] == 0.05
    assert snap["theta"] == -0.08
    assert snap["vega"] == 0.15
    assert snap["implied_volatility"] == 0.35
    assert snap["bid"] == 2.50
    assert snap["ask"] == 2.60
    assert snap["mid"] == pytest.approx(2.55)
    assert snap["spread_pct"] == pytest.approx((2.60 - 2.50) / 2.55)


async def test_get_option_snapshots_returns_keyed_dict(client):
    fake_resp = {
        "A": _fake_snapshot(delta=0.30),
        "B": _fake_snapshot(delta=0.40),
    }
    client.option_data = MagicMock()
    client.option_data.get_option_snapshot = MagicMock(return_value=fake_resp)

    snaps = await client.get_option_snapshots(["A", "B"])

    assert set(snaps.keys()) == {"A", "B"}
    assert snaps["A"]["delta"] == 0.30
    assert snaps["B"]["delta"] == 0.40


async def test_get_option_snapshots_empty_input_short_circuits(client):
    client.option_data = MagicMock()
    client.option_data.get_option_snapshot = MagicMock()

    snaps = await client.get_option_snapshots([])

    assert snaps == {}
    client.option_data.get_option_snapshot.assert_not_called()


async def test_get_option_snapshot_handles_missing_greeks(client):
    fake = SimpleNamespace(
        greeks=None,
        implied_volatility=None,
        latest_quote=SimpleNamespace(bid_price=1.0, ask_price=1.2,
                                     bid_size=1, ask_size=1),
        latest_trade=None,
        daily_bar=None,
        open_interest=None,
    )
    client.option_data = MagicMock()
    client.option_data.get_option_snapshot = MagicMock(return_value={"X": fake})

    snap = await client.get_option_snapshot("X")

    assert snap is not None
    assert snap["delta"] is None
    assert snap["bid"] == 1.0
    assert snap["ask"] == 1.2
    assert snap["mid"] == pytest.approx(1.1)


async def test_submit_option_limit_order_uses_limit_request(client):
    captured: dict = {}

    def fake_submit(req):
        captured["req"] = req
        return SimpleNamespace(
            id="broker-1",
            client_order_id="opt_TEST_x",
            symbol="AAPL260619C00200000",
            status=SimpleNamespace(value="accepted"),
            qty=2,
        )

    client.trading = MagicMock()
    client.trading.submit_order = MagicMock(side_effect=fake_submit)

    result = await client.submit_option_limit_order(
        contract_symbol="AAPL260619C00200000",
        qty=2, side="buy", limit_price=2.55,
        client_order_id="opt_TEST_x",
    )

    assert result["id"] == "broker-1"
    assert result["status"] == "accepted"
    req = captured["req"]
    assert float(req.limit_price) == 2.55


async def test_cancel_option_order_passes_through(client):
    client.trading = MagicMock()
    client.trading.cancel_order_by_id = MagicMock()

    await client.cancel_option_order("broker-xyz")

    client.trading.cancel_order_by_id.assert_called_once_with("broker-xyz")
