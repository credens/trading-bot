"""
Signal computation for scalping bots.
VWAP + EMA(9/21) + RSI(14) + ATR(14) + Order Book Imbalance + Spread
"""
import numpy as np
import pandas as pd
import logging

log = logging.getLogger(__name__)


def klines_df(raw):
    df = pd.DataFrame(raw, columns=[
        "ts","open","high","low","close","volume",
        "ct","qv","trades","tb","tbq","ig"])
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    return df


def vwap(df, periods=30):
    d = df.tail(periods)
    tp = (d["high"] + d["low"] + d["close"]) / 3
    return float((tp * d["volume"]).sum() / d["volume"].sum())


def ema(closes, span):
    return float(pd.Series(closes).ewm(span=span, adjust=False).mean().iloc[-1])


def rsi(closes, period=14):
    s = pd.Series(closes)
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def atr(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def book_imbalance(client, symbol, depth_pct=0.001):
    """
    Ratio of bid volume to ask volume within depth_pct of mid.
    >1.3 = buying pressure (bullish), <0.77 = selling pressure (bearish).
    """
    try:
        book = client.futures_order_book(symbol=symbol, limit=20)
        bid0 = float(book["bids"][0][0])
        ask0 = float(book["asks"][0][0])
        mid  = (bid0 + ask0) / 2
        lo   = mid * (1 - depth_pct)
        hi   = mid * (1 + depth_pct)
        bid_vol = sum(float(q) for p, q in book["bids"] if float(p) >= lo)
        ask_vol = sum(float(q) for p, q in book["asks"] if float(p) <= hi)
        return bid_vol / ask_vol if ask_vol > 0 else 99.0
    except Exception:
        return 1.0  # neutral on error


def spread_pct(client, symbol):
    try:
        book = client.futures_order_book(symbol=symbol, limit=5)
        bid = float(book["bids"][0][0])
        ask = float(book["asks"][0][0])
        return (ask - bid) / ((bid + ask) / 2) * 100
    except Exception:
        return 0.1


def evaluate(client, symbol, cfg):
    """
    Compute all signals and return action + context dict.

    cfg keys:
      max_spread_pct, min_atr_pct, max_spike_mult,
      rsi_long_lo, rsi_long_hi, rsi_short_lo, rsi_short_hi,
      ob_threshold
    """
    raw = client.futures_klines(symbol=symbol, interval="1m", limit=50)
    df  = klines_df(raw)
    cls = df["close"].tolist()
    price = cls[-1]

    v    = vwap(df)
    e9   = ema(cls, 9)
    e21  = ema(cls, 21)
    r    = rsi(cls)
    a    = atr(df)
    a_pct = a / price * 100
    spr  = spread_pct(client, symbol)
    ob   = book_imbalance(client, symbol)
    last_range = float(df["high"].iloc[-1] - df["low"].iloc[-1])
    spike_mult = last_range / a if a > 0 else 0

    ctx = dict(price=price, vwap=v, ema9=e9, ema21=e21, rsi=r,
               atr_pct=a_pct, spread_pct=spr, ob=ob, spike_mult=spike_mult)

    # ── Hard filters ──
    if spr > cfg["max_spread_pct"]:
        return "FLAT", f"spread {spr:.3f}% > {cfg['max_spread_pct']}%", ctx
    if a_pct < cfg["min_atr_pct"]:
        return "FLAT", f"ATR {a_pct:.3f}% too small", ctx
    if spike_mult > cfg["max_spike_mult"]:
        return "FLAT", f"volatility spike {spike_mult:.1f}x", ctx

    # ── Signals ──
    long = (price > v and e9 > e21
            and cfg["rsi_long_lo"] <= r <= cfg["rsi_long_hi"]
            and ob >= cfg["ob_threshold"])
    short = (price < v and e9 < e21
             and cfg["rsi_short_lo"] <= r <= cfg["rsi_short_hi"]
             and ob <= 1 / cfg["ob_threshold"])

    if long:
        return "LONG",  f"VWAP✓ EMA✓ RSI:{r:.0f} OB:{ob:.2f}", ctx
    if short:
        return "SHORT", f"VWAP✓ EMA✓ RSI:{r:.0f} OB:{ob:.2f}", ctx
    return "FLAT", f"no setup | RSI:{r:.0f} OB:{ob:.2f}", ctx
