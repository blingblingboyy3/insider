"""Company context for an alert: price now and before, market cap, industry — all from free sources.

Prices: Yahoo Finance's public chart endpoint (no key). Shares outstanding and industry: the SEC's own
company data. Everything is cached per run so a cluster of filings on one company costs one lookup."""
import json
import math
import time
import urllib.request
from datetime import datetime, timezone

from edgar import get as sec_get

YF = "https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=1y&interval=1d"
_CACHE = {}


def _yahoo(ticker):
    if ticker in _CACHE:
        return _CACHE[ticker]
    out = None
    for host in ("query1", "query2"):
        try:
            req = urllib.request.Request(YF.format(t=ticker).replace("query1", host), headers={"User-Agent": "Mozilla/5.0 (Insider alerts)"})
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.loads(r.read().decode()).get("chart", {}).get("result") or []
            if res:
                out = res[0]
                break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    _CACHE[ticker] = out
    return out


def price_history(ticker):
    """[(date, close)] for the last year, oldest first; [] if the ticker is unknown."""
    r = _yahoo(ticker)
    if not r:
        return []
    ts = r.get("timestamp") or []
    closes = (r.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    return [(datetime.fromtimestamp(t, timezone.utc).date(), c) for t, c in zip(ts, closes) if c is not None]


def price_on(hist, day):
    """Close on `day` or the last trading day before it."""
    best = None
    for d, c in hist:
        if d <= day:
            best = c
        else:
            break
    return best


def change(hist, days):
    if len(hist) < 2:
        return None
    now = hist[-1][1]
    then = price_on(hist, hist[-1][0].fromordinal(hist[-1][0].toordinal() - days))
    return (now / then - 1.0) if then else None


def shares_outstanding(cik):
    for concept in ("dei/EntityCommonStockSharesOutstanding", "us-gaap/CommonStockSharesOutstanding", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding"):
        try:
            data = json.loads(sec_get(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{int(cik):010d}/{concept}.json").decode())
            vals = sorted(data.get("units", {}).get("shares", []), key=lambda v: v.get("end", ""))
            if vals and vals[-1]["val"]:
                return float(vals[-1]["val"])
        except Exception:  # noqa: BLE001
            continue
    return None


def yahoo_cap(ticker):
    r = _yahoo(ticker)
    try:
        return float(r["meta"].get("marketCap") or 0) or None
    except Exception:  # noqa: BLE001
        return None


def company_facts(cik):
    """{"industry", "exchange", "state", "name"} from the SEC's submissions file."""
    try:
        data = json.loads(sec_get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json").decode())
        ex = data.get("exchanges") or []
        return {"industry": data.get("sicDescription", ""), "sic": data.get("sic", ""), "exchange": ex[0] if ex else "",
                "state": data.get("stateOfIncorporation", ""), "name": data.get("name", ""), "tickers": data.get("tickers") or []}
    except Exception:  # noqa: BLE001
        return {"industry": "", "sic": "", "exchange": "", "state": "", "name": "", "tickers": []}


def cap_tier(market_cap):
    if not market_cap:
        return "unknown"
    if market_cap < 300e6:
        return "micro"
    if market_cap < 2e9:
        return "small"
    if market_cap < 10e9:
        return "mid"
    if market_cap < 200e9:
        return "large"
    return "mega"


def context(ticker, cik):
    """Everything the scorer and the phone page want to know about the company right now."""
    hist = price_history(ticker) if ticker else []
    now = hist[-1][1] if hist else None
    shares = shares_outstanding(cik) if cik else None
    cap = (now * shares) if (now and shares) else yahoo_cap(ticker)
    facts = company_facts(cik) if cik else {}
    hi = max((c for _, c in hist), default=None)
    lo = min((c for _, c in hist), default=None)
    return {
        "price": now, "as_of": hist[-1][0].isoformat() if hist else None,
        "chg_30d": change(hist, 30), "chg_90d": change(hist, 90), "chg_1y": change(hist, 365),
        "high_52w": hi, "low_52w": lo, "off_high": (now / hi - 1.0) if (now and hi) else None,
        "shares_out": shares, "market_cap": cap, "tier": cap_tier(cap),
        "industry": facts.get("industry", ""), "exchange": facts.get("exchange", ""), "state": facts.get("state", ""),
        "history": [(d.isoformat(), round(c, 4)) for d, c in hist[-130:]],   # ~6 months for the sparkline
    }


def fmt_money(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    a = abs(v)
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if a >= div:
            return f"${v / div:.2f}{unit}".replace(".00", "")
    return f"${v:,.0f}"
