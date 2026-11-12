import numpy as np
import pandas as pd
import pytest

from backend.analysis import indicators as ind

# Wilder RSI reference data (StockCharts "RSI" ChartSchool example).
RSI_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
]


def _wilder_rsi_by_hand(closes, n=14):
    d = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    g = sum(max(x, 0) for x in d[:n]) / n
    l = sum(max(-x, 0) for x in d[:n]) / n
    out = [100 - 100 / (1 + g / l)]
    for x in d[n:]:
        g = (g * (n - 1) + max(x, 0)) / n
        l = (l * (n - 1) + max(-x, 0)) / n
        out.append(100 - 100 / (1 + g / l))
    return out


def test_rsi_matches_wilder_reference():
    rsi = ind.rsi(pd.Series(RSI_CLOSES), 14)
    assert rsi.iloc[:14].isna().all()
    # StockCharts publishes 70.53 / 66.32 because its sheet rounds the averages
    # to 2 decimals; the exact Wilder values are 70.46 / 66.25.
    assert rsi.iloc[14] == pytest.approx(70.46, abs=0.01)
    assert rsi.iloc[15] == pytest.approx(66.25, abs=0.01)
    assert list(rsi.iloc[14:]) == pytest.approx(_wilder_rsi_by_hand(RSI_CLOSES))


def test_ema_seed_and_recursion():
    s = pd.Series(np.arange(1, 31, dtype=float))
    e = ind.ema(s, 10)
    assert e.iloc[:9].isna().all()
    assert e.iloc[9] == pytest.approx(5.5)                 # SMA of 1..10
    alpha = 2 / 11
    assert e.iloc[10] == pytest.approx(alpha * 11 + (1 - alpha) * 5.5)


def test_atr_hand_calculation():
    high = pd.Series([10, 11, 12, 11.5])
    low = pd.Series([9, 10, 10.5, 10])
    close = pd.Series([9.5, 10.8, 11.0, 10.2])
    tr = ind.true_range(high, low, close)
    # TR: 1.0, max(1, 1.5, 0.5)=1.5, max(1.5, 1.2, 0.3)=1.5, max(1.5, 0.5, 1.0)=1.5
    assert list(tr) == pytest.approx([1.0, 1.5, 1.5, 1.5])
    atr = ind.atr(high, low, close, 3)
    assert atr.iloc[2] == pytest.approx((1.0 + 1.5 + 1.5) / 3)
    assert atr.iloc[3] == pytest.approx((atr.iloc[2] * 2 + 1.5) / 3)


def test_bollinger_matches_numpy():
    rng = np.random.default_rng(1)
    s = pd.Series(100 + rng.normal(0, 1, 60).cumsum())
    bb = ind.bollinger(s, 20, 2)
    window = s.iloc[-20:].to_numpy()
    assert bb["bb_mid"].iloc[-1] == pytest.approx(window.mean())
    assert bb["bb_upper"].iloc[-1] == pytest.approx(window.mean() + 2 * window.std(ddof=0))


def test_macd_is_ema_difference():
    s = pd.Series(np.linspace(100, 130, 80) + np.sin(np.arange(80)))
    m = ind.macd(s)
    expected = ind.ema(s, 12) - ind.ema(s, 26)
    pd.testing.assert_series_equal(m["macd"], expected, check_names=False)
    assert m["macd_signal"].first_valid_index() == 25 + 8   # 26-bar seed, then 9-bar signal seed


def test_adx_trend_behaviour():
    n = 120
    up_high = pd.Series(np.linspace(100, 160, n) + 1)
    up_low = up_high - 2
    up_close = up_high - 0.5
    a = ind.adx(up_high, up_low, up_close, 14)
    assert a["plus_di"].iloc[-1] > a["minus_di"].iloc[-1]
    assert a["adx"].iloc[-1] > 40
    assert a["adx"].dropna().between(0, 100).all()


def test_compute_all_requires_history_for_ema200():
    n = 120
    df = pd.DataFrame({"open": 1.0, "high": np.linspace(2, 3, n), "low": np.linspace(1, 2, n),
                       "close": np.linspace(1.5, 2.5, n), "tick_volume": 1, "spread": 1})
    out = ind.compute_all(df)
    assert out["ema200"].isna().all()
    assert not np.isnan(out["ema50"].iloc[-1])


def test_volatility_regime():
    atr = pd.Series([1.0] * 99 + [2.0])
    assert ind.volatility_regime(atr)["regime"] == "HIGH"
    atr = pd.Series([1.0] * 99 + [0.5])
    assert ind.volatility_regime(atr)["regime"] == "LOW"
    assert ind.volatility_regime(pd.Series([1.0] * 5))["regime"] is None
