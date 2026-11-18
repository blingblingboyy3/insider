"""Turning a pile of insider buys into a score you can act on — and explaining every point of it.

The score follows what the research on insider trading has actually found predictive:
  clusters of insiders buying together  >  one insider buying
  CEO / CFO / chair buys                 >  director or fund buys
  buys that are big for that person     >  token buys
  smaller companies                      >  megacaps
  buying after a drop                    >  buying at highs
and it marks down what the research says is noise: pre-scheduled (10b5-1) purchases and fund entities topping up.
Selling is ignored on purpose; it has no reliable signal.
"""
from collections import defaultdict
from datetime import date

from edgar import looks_like_fund, role_of

CLUSTER_DAYS = 30
STRONG, NOTABLE = 55, 40
ROLE_POINTS = {"CEO": 20, "CFO": 20, "Chair": 18, "President": 16, "COO": 14, "C-suite": 12, "General Counsel": 10, "VP": 8,
               "Officer": 8, "Director": 8, "10% owner": 3, "Insider": 4}


def _d(s):
    return date.fromisoformat(s[:10])


def group_buys(buys):
    """Buys grouped by company ticker (falls back to CIK), newest first inside each group."""
    g = defaultdict(list)
    for b in buys:
        key = b["company"]["ticker"] or ("cik" + b["company"]["cik"])
        g[key].append(b)
    for k in g:
        g[k].sort(key=lambda b: b["trade_date"], reverse=True)
    return g


def person_key(b):
    return (b["owner"].get("cik") or b["owner"].get("name", "")).lower()


def score_company(buys, ctx, today=None):
    """Score one company's recent open-market buys. Returns a dict with score, label, reasons, and the rolled-up facts."""
    today = today or date.today()
    recent = [b for b in buys if (today - _d(b["trade_date"])).days <= CLUSTER_DAYS]
    if not recent:
        return None
    people = {}
    for b in recent:
        k = person_key(b)
        p = people.setdefault(k, {"name": b["owner"]["name"], "role": role_of(b["owner"]), "shares": 0.0, "value": 0.0, "owned_after": 0.0,
                                  "fund": looks_like_fund(b["owner"]["name"]) and not b["owner"].get("officer") and not b["owner"].get("director"),
                                  "plan": False, "direct": False, "first": b["trade_date"], "last": b["trade_date"], "buys": 0, "prices": []})
        p["shares"] += b["shares"]
        p["value"] += b["value"]
        p["owned_after"] = max(p["owned_after"], b["owned_after"])
        p["plan"] = p["plan"] or b["plan_10b5_1"]
        p["direct"] = p["direct"] or b["direct"]
        p["first"], p["last"] = min(p["first"], b["trade_date"]), max(p["last"], b["trade_date"])
        p["buys"] += 1
        p["prices"].append(b["price"])
    for p in people.values():
        before = max(p["owned_after"] - p["shares"], 0.0)
        p["stake_increase"] = (p["shares"] / before) if before > 0 else None      # None = started from zero
        p["avg_price"] = round(sum(p["prices"]) / len(p["prices"]), 4)
    humans = [p for p in people.values() if not p["fund"]]
    reasons, pts = [], {}

    # cluster
    n = len(humans)
    pts["cluster"] = 40 if n >= 3 else 25 if n == 2 else 0
    if n >= 2:
        reasons.append(f"{n} different insiders bought within {CLUSTER_DAYS} days — clusters are the strongest version of this signal")
    elif n == 1:
        reasons.append("A single insider buying; stronger if others follow")
    # role
    best_role = max((p["role"] for p in humans), key=lambda r: ROLE_POINTS.get(r, 0), default=None)
    pts["role"] = ROLE_POINTS.get(best_role, 0) if best_role else 0
    if best_role in ("CEO", "CFO", "Chair", "President", "COO"):
        reasons.append(f"The {best_role} is among the buyers — top executives' buys carry the most information")
    elif best_role:
        reasons.append(f"Highest-ranking buyer: {best_role}")
    # conviction: the largest human buy by value and by stake increase
    big = max(humans, key=lambda p: p["value"], default=None)
    pts["size"] = 0
    if big:
        v = big["value"]
        pts["size"] += 15 if v >= 1e6 else 10 if v >= 250e3 else 6 if v >= 100e3 else 3 if v >= 25e3 else 0
        inc = max((p["stake_increase"] for p in humans if p["stake_increase"] is not None), default=None)
        if inc is None and any(p["stake_increase"] is None for p in humans):
            pts["size"] += 8
            reasons.append("At least one buyer is starting a position from nothing")
        elif inc is not None:
            pts["size"] += 10 if inc >= 0.5 else 6 if inc >= 0.2 else 3 if inc >= 0.05 else 0
            if inc >= 0.2:
                reasons.append(f"Biggest stake increase: +{inc:.0%} of what that insider already held — a real commitment, not a token buy")
            elif inc < 0.05:
                reasons.append("Small relative to what the buyers already own")
        if v >= 250e3:
            reasons.append(f"Largest single buyer put in {_money(v)}")
    # company size
    pts["cap"] = {"micro": 6, "small": 10, "mid": 6, "large": 2, "mega": 0}.get(ctx.get("tier", "unknown"), 3)
    if ctx.get("tier") in ("small", "micro"):
        reasons.append(f"{ctx['tier'].title()}-cap company ({_money(ctx.get('market_cap'))}) — insider buys matter more where fewer analysts look")
    elif ctx.get("tier") in ("large", "mega"):
        reasons.append(f"{ctx['tier'].title()}-cap ({_money(ctx.get('market_cap'))}) — insider buys move the needle less here")
    # buying the dip
    pts["dip"] = 0
    off = ctx.get("off_high")
    c30 = ctx.get("chg_30d")
    if off is not None and off <= -0.30:
        pts["dip"] += 10
        reasons.append(f"Stock is {abs(off):.0%} below its 52-week high — insiders buying into a drawdown")
    elif off is not None and off <= -0.15:
        pts["dip"] += 6
        reasons.append(f"Stock is {abs(off):.0%} off its 52-week high")
    if c30 is not None and c30 <= -0.10:
        pts["dip"] += 4
        reasons.append(f"Down {abs(c30):.0%} in the last 30 days")
    # penalties
    pts["penalty"] = 0
    if humans and all(p["plan"] for p in humans):
        pts["penalty"] -= 10
        reasons.append("Pre-scheduled 10b5-1 plan purchases — these carry little information")
    if not humans:
        pts["penalty"] -= 15
        reasons.append("Only fund/10%-owner entities bought — treated as weak")
    elif humans and not any(p["direct"] for p in humans):
        pts["penalty"] -= 3
        reasons.append("Held indirectly (trusts/entities)")
    score = max(0, min(100, sum(pts.values())))
    label = "Strong" if score >= STRONG else "Notable" if score >= NOTABLE else "Weak"
    total_value = sum(p["value"] for p in people.values())
    return {
        "score": score, "label": label, "points": pts, "reasons": reasons,
        "buyers": sorted(people.values(), key=lambda p: -p["value"]),
        "n_insiders": n, "n_funds": len(people) - n, "total_value": round(total_value, 2),
        "first_trade": min(p["first"] for p in people.values()), "last_trade": max(p["last"] for p in people.values()),
        "avg_price": round(sum(b["value"] for b in recent) / max(sum(b["shares"] for b in recent), 1), 4),
        "take": take_for(label, n, best_role, ctx),
    }


def take_for(label, n, role, ctx):
    """A plain-English read of what this pattern has historically meant. Information, not instructions."""
    if label == "Strong":
        head = "This is the pattern with the best track record: " + ("several insiders " if n >= 2 else "a senior insider ") + "buying on the open market"
        if ctx.get("off_high") is not None and ctx["off_high"] <= -0.15:
            head += " into a drawdown"
        head += "."
        body = ("Studies of this setup show modest average outperformance over the following 6–12 months, with wide dispersion — most names drift, "
                "a few carry the result. People who use it treat each alert as one position in a basket of many, sized small, held for months, "
                "and never as a reason to buy options or go all-in.")
    elif label == "Notable":
        head = "Worth a look, not a conviction signal."
        body = ("One or two of the ingredients are here, but not the full cluster. Historically these do slightly better than average; "
                "the usual approach is to watch for more insiders to follow within the month before acting.")
    else:
        head = "Logged for context only."
        body = "Insider buying without the supporting factors (cluster, senior role, meaningful size) has not shown a reliable edge."
    return {"headline": head, "body": body, "horizon": "6–12 months", "studied_edge": "a few % over the market, on average, per year"}


def _money(v):
    if v is None:
        return "—"
    a = abs(v)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if a >= div:
            return f"${v / div:.1f}{unit}".replace(".0", "")
    return f"${v:,.0f}"
