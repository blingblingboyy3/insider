"""The fine print behind an alert: each insider's individual trades, how they hold the stock, what else they did
in the same filings (options, grants, sales), and their own track record at this company."""
from datetime import date, timedelta

from edgar import describe, is_open_market_buy, is_open_market_sale, insider_history, role_of

PERSON_WINDOW = 30


def _pk(b):
    return (b["owner"].get("cik") or b["owner"].get("name", "")).lower()


def buyers(summary_buyers, grp, events, ctx):
    """Attach per-trade detail to each buyer summary from score_company."""
    by_person = {}
    for b in grp:
        by_person.setdefault(_pk(b), []).append(b)
    ev_by_person = {}
    for e in events:
        if e["company"]["cik"] == grp[0]["company"]["cik"]:
            ev_by_person.setdefault(_pk(e), []).append(e)
    shares_out = ctx.get("shares_out") or 0
    out = []
    for p in summary_buyers:
        key = next((k for k, v in by_person.items() if v and v[0]["owner"]["name"] == p["name"]), None)
        mine = sorted(by_person.get(key, []), key=lambda b: b["trade_date"], reverse=True)
        owner = mine[0]["owner"] if mine else {}
        recent = [b for b in mine if (date.today() - date.fromisoformat(b["trade_date"][:10])).days <= PERSON_WINDOW]
        trades = [{"date": b["trade_date"], "shares": b["shares"], "price": b["price"], "value": b["value"], "direct": b["direct"],
                   "nature": b.get("nature", ""), "plan": b.get("plan_10b5_1", False), "notes": b.get("notes", "")[:220], "adsh": b["adsh"],
                   "security": b.get("security", ""), "type": describe(b)} for b in recent]
        evs = sorted(ev_by_person.get(key, []), key=lambda e: e["trade_date"], reverse=True)
        same = [e for e in evs if e["adsh"] in {b["adsh"] for b in recent}]   # what else was in the same filings
        also = [{"date": e["trade_date"], "type": describe(e), "code": e.get("code"), "shares": e["shares"], "price": e["price"], "value": e["value"],
                 "security": e.get("security", ""), "exercise_price": e.get("exercise_price"), "expires": e.get("expires"), "derivative": bool(e.get("derivative")),
                 "ad": e.get("ad"), "notes": e.get("notes", "")[:160]} for e in same[:8]]
        owned = p.get("owned_after") or 0
        out.append(dict(p, title=owner.get("title") or "", flags={"director": owner.get("director"), "officer": owner.get("officer"), "ten_percent": owner.get("ten_percent")},
                        cik=owner.get("cik", ""), trades=trades, also=also,
                        pct_company=(owned / shares_out) if (shares_out and owned) else None,
                        holding=("direct" if all(t["direct"] for t in trades) else "indirect" if trades and not any(t["direct"] for t in trades) else "mixed"),
                        natures=sorted({t["nature"] for t in trades if t["nature"]})))
    return out


def sells(cik, events, today):
    """Insider open-market sales at this company in the last 90 days: how many, how much, who."""
    cutoff = (today - timedelta(days=90)).isoformat()
    s = [e for e in events if e["company"]["cik"] == cik and is_open_market_sale(e) and e["trade_date"] >= cutoff]
    who = {}
    for e in s:
        w = who.setdefault(e["owner"]["name"], {"name": e["owner"]["name"], "role": role_of(e["owner"]), "value": 0.0, "shares": 0.0, "last": e["trade_date"]})
        w["value"] += e["value"]
        w["shares"] += e["shares"]
        w["last"] = max(w["last"], e["trade_date"])
    return {"n": len(s), "value": round(sum(e["value"] for e in s), 2), "sellers": sorted(who.values(), key=lambda w: -w["value"])[:6]}


def histories(buyer_list, issuer_cik, people, today, max_people=4):
    """Fill each buyer's 12-month record at this company from their own SEC filings (cached ~60 days in `people`)."""
    for p in [b for b in buyer_list if not b.get("fund")][:max_people]:
        cik = p.get("cik")
        if not cik:
            continue
        k = f"{cik}|{issuer_cik}"
        rec = people.get(k)
        if not rec or rec.get("as_of", "") < (today - timedelta(days=7)).isoformat():
            try:
                tx = insider_history(cik, issuer_cik, limit=8)
            except Exception:  # noqa: BLE001
                tx = []
            rec = people[k] = {"as_of": today.isoformat(), "tx": [{"date": t["trade_date"], "code": t["code"], "ad": t.get("ad"), "shares": t["shares"], "price": t["price"],
                                                                 "value": t["value"], "type": describe(t), "derivative": bool(t.get("derivative")), "adsh": t["adsh"],
                                                                 "owned_after": t["owned_after"]} for t in tx]}
        tx = rec["tx"]
        buys_ = [t for t in tx if t["code"] == "P" and t["ad"] == "A" and not t["derivative"]]
        sells_ = [t for t in tx if t["code"] == "S" and t["ad"] == "D" and not t["derivative"]]
        grants = [t for t in tx if t["code"] == "A"]
        exer = [t for t in tx if t["code"] in ("M", "X")]
        p["history"] = {
            "months": 12, "filings": len({t["adsh"] for t in tx}),
            "buys": len(buys_), "buy_value": round(sum(t["value"] for t in buys_), 2),
            "sells": len(sells_), "sell_value": round(sum(t["value"] for t in sells_), 2), "last_sale": max((t["date"] for t in sells_), default=None),
            "grants": len(grants), "exercises": len(exer),
            "timeline": sorted(({"date": t["date"], "type": t["type"], "shares": t["shares"], "price": t["price"], "value": t["value"], "ad": t["ad"]} for t in tx
                                if not t["derivative"] or t["code"] in ("A", "M", "X")), key=lambda t: t["date"], reverse=True)[:14],
            "read": _read(buys_, sells_, grants),
        }
    return buyer_list


def _read(buys_, sells_, grants):
    if sells_ and not buys_[1:]:
        return "First open-market buy this year after selling earlier — a change of mind, or a required plan buy; read the notes"
    if len(buys_) >= 3 and not sells_:
        return f"Repeat buyer: {len(buys_)} open-market buys this year and no sales — consistent conviction"
    if buys_ and not sells_:
        return "Buying with no sales in the past year"
    if buys_ and sells_:
        return f"Mixed: {len(buys_)} buy(s) and {len(sells_)} sale(s) this year"
    if grants and not buys_:
        return "Mostly stock awards this year; this is the first cash purchase on record"
    return "No prior filings at this company in the past year"
