"""Order options come from the exchange per token, not a hardcode (issue #1295).

Copy-trade orders on ordinary binary markets were rejected because the
order path always signed with neg_risk=True (the weather-market default).
"""
from unittest.mock import MagicMock

from src.execution.live_trader import LiveTrader


def _trader(neg_risk: bool) -> LiveTrader:
    client = MagicMock()
    client.get_tick_size.return_value = "0.01"
    client.get_neg_risk.return_value = neg_risk
    client.create_and_post_order.return_value = {"orderID": "0xabc"}
    return LiveTrader(client)


def test_place_order_uses_neg_risk_false_for_ordinary_market():
    trader = _trader(neg_risk=False)
    trader.place_order(
        "tok-binary", "YES", 50, 5.0, ticker="0xcond", station="",
    )
    _, options = trader.client.create_and_post_order.call_args.args
    assert options.neg_risk is False
    trader.client.get_neg_risk.assert_called_with("tok-binary")


def test_place_order_uses_neg_risk_true_for_neg_risk_market():
    trader = _trader(neg_risk=True)
    trader.place_order(
        "tok-negrisk", "YES", 50, 5.0, ticker="0xcond", station="",
    )
    _, options = trader.client.create_and_post_order.call_args.args
    assert options.neg_risk is True


def test_order_options_tick_size_comes_from_exchange():
    trader = _trader(neg_risk=False)
    trader.client.get_tick_size.return_value = "0.001"
    options = trader._order_options("tok-fine")
    assert options.tick_size == "0.001"
    trader.client.get_tick_size.assert_called_with("tok-fine")
