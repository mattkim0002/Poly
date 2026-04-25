"""Traditional market data via yfinance — SPY, VIX, DXY, Gold.

Provides a lightweight snapshot of traditional-market conditions for
the macro bias module. All functions swallow errors and return neutral
defaults so the bot never stalls on a Yahoo Finance timeout.

Cache: 5-minute TTL per ticker to avoid rate limits.
"""

import time
from utils.logger import log

_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 300


def _get_ticker_snapshot(symbol: str) -> dict | None:
    now = time.time()
    cached = _cache.get(symbol)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
        hist = tk.history(period="2d", interval="1h")
        if hist.empty:
            return None

        current = float(hist["Close"].iloc[-1])
        prev_close = float(hist["Close"].iloc[0])
        high_24h = float(hist["High"].max())
        low_24h = float(hist["Low"].min())
        change_pct = ((current - prev_close) / prev_close) * 100 if prev_close else 0.0

        result = {
            "symbol": symbol,
            "price": round(current, 2),
            "change_pct": round(change_pct, 2),
            "high_24h": round(high_24h, 2),
            "low_24h": round(low_24h, 2),
        }
        _cache[symbol] = (now, result)
        return result
    except Exception as e:
        log.debug("yfinance fetch failed for %s: %s", symbol, e)
        return None


TRAD_TICKERS = {
    "SPY": "SPY",
    "VIX": "^VIX",
    "DXY": "DX-Y.NYB",
    "GOLD": "GC=F",
    "BTC_YF": "BTC-USD",
}


def get_market_snapshot() -> dict:
    """Return a dict of traditional market snapshots. Never raises."""
    result = {}
    for label, symbol in TRAD_TICKERS.items():
        snap = _get_ticker_snapshot(symbol)
        if snap:
            result[label] = snap
    return result


def get_market_summary() -> str:
    """One-line summary string for logging/display."""
    snap = get_market_snapshot()
    if not snap:
        return "traditional markets: no data"
    parts = []
    for label in ["SPY", "VIX", "DXY", "GOLD", "BTC_YF"]:
        s = snap.get(label)
        if s:
            parts.append(f"{label}=${s['price']:.0f}({s['change_pct']:+.1f}%)")
    return " | ".join(parts)


def get_risk_off_signal() -> dict:
    """Detect risk-off conditions from traditional markets.

    Returns {"risk_off": bool, "signals": list[str], "vix": float|None}.
    Risk-off = 2+ signals agree (VIX spike + SPY drop + USD strength).
    """
    snap = get_market_snapshot()
    signals = []
    vix_level = None

    vix = snap.get("VIX")
    if vix:
        vix_level = vix["price"]
        if vix["price"] > 25:
            signals.append(f"VIX elevated at {vix['price']:.1f}")
        if vix["change_pct"] > 10:
            signals.append(f"VIX spiking +{vix['change_pct']:.1f}%")

    spy = snap.get("SPY")
    if spy and spy["change_pct"] < -1.0:
        signals.append(f"SPY down {spy['change_pct']:.1f}%")

    dxy = snap.get("DXY")
    if dxy and dxy["change_pct"] > 0.5:
        signals.append(f"USD strengthening +{dxy['change_pct']:.1f}%")

    return {
        "risk_off": len(signals) >= 2,
        "signals": signals,
        "vix": vix_level,
    }
