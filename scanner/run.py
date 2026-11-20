"""One scan: new Form 4s → open-market buys → scores → pushes → data files for the phone page.

Runs every 10 minutes on GitHub Actions. All state lives in docs/data/*.json, committed back to the repo, so
the page and the scanner share one source of truth and nothing needs a database.

    python scanner/run.py            # a real run
    python scanner/run.py --dry      # no pushes
    python scanner/run.py --days 14  # look back further (first run / after an outage)
"""
import argparse
import json
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import edgar  # noqa: E402
import market  # noqa: E402
import notify  # noqa: E402
import score  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("INSIDER_DATA") or (ROOT / "docs" / "data"))
KEEP_BUYS_DAYS = 120
KEEP_ALERTS_DAYS = 400
MAX_FILINGS_PER_RUN = 400
MAX_PUSHES_PER_RUN = 5
REALERT_GAP_DAYS = 30


def load(name, default):
    p = DATA / name
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def save(name, obj):
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / name).write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))


def compact(b):
    """The fields of a buy worth keeping around."""
    return {k: b[k] for k in ("adsh", "filed", "trade_date", "code", "shares", "price", "value", "owned_after", "direct", "plan_10b5_1", "company", "owner")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--days", type=int, default=0, help="how far back to look for filings (default: since last run, min 3)")
    ap.add_argument("--fixtures", help="folder of Form 4 XML files to use instead of EDGAR (tests)")
    ap.add_argument("--no-market", action="store_true", help="skip price/market-cap lookups (tests)")
    ap.add_argument("--rescore", action="store_true", help="re-score every company on file (after a scoring change)")
    args = ap.parse_args()
    t0 = time.time()
    today = datetime.now(timezone.utc).date()
    seen = set(load("seen.json", []))
    buys = load("buys.json", [])
    alerts = load("alerts.json", [])
    runs = load("runs.json", [])
    last_run = runs[-1]["at"][:10] if runs else None
    since = today - timedelta(days=args.days or (max(3, (today - date.fromisoformat(last_run)).days + 2) if last_run else 14))
    log = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "since": since.isoformat(), "filings": 0, "new": 0, "buys": 0, "alerts": [], "errors": 0}

    # 1. new filings ------------------------------------------------------------------------------
    new_buys = []
    if args.fixtures:
        for p in sorted(Path(args.fixtures).glob("*.xml")):
            recs = edgar.parse_form4(p.read_bytes(), adsh=p.stem, filed=today.isoformat())
            new_buys += [r for r in recs if edgar.is_open_market_buy(r)]
            log["filings"] += 1
            log["new"] += 1
    else:
        try:
            filings = edgar.recent_filings(since, today)
        except Exception as e:  # noqa: BLE001
            print("full-text search failed, falling back to the latest-filings feed:", e)
            filings = edgar.current_filings()
        log["filings"] = len(filings)
        todo = [f for f in filings if f["adsh"] not in seen][:MAX_FILINGS_PER_RUN]
        for f in todo:
            try:
                recs = edgar.parse_form4(edgar.filing_xml(f), adsh=f["adsh"], filed=f["filed"])
            except Exception as e:  # noqa: BLE001
                log["errors"] += 1
                print("skip", f["adsh"], e)
                continue
            seen.add(f["adsh"])
            log["new"] += 1
            new_buys += [r for r in recs if edgar.is_open_market_buy(r)]
    log["buys"] = len(new_buys)
    print(f"{log['new']} new filings, {len(new_buys)} open-market buys")

    # 2. merge, trim ------------------------------------------------------------------------------
    have = {(b["adsh"], b["owner"].get("cik") or b["owner"].get("name"), b["trade_date"], b["shares"]) for b in buys}
    for b in new_buys:
        k = (b["adsh"], b["owner"].get("cik") or b["owner"].get("name"), b["trade_date"], b["shares"])
        if k not in have:
            buys.append(compact(b))
            have.add(k)
    cutoff = (today - timedelta(days=KEEP_BUYS_DAYS)).isoformat()
    buys = [b for b in buys if b["trade_date"] >= cutoff]

    # 3. score every company that got a new buy -----------------------------------------------------
    groups = score.group_buys(buys)
    touched = {(b["company"]["ticker"] or "cik" + b["company"]["cik"]) for b in new_buys}
    companies = load("companies.json", {})
    if args.rescore or not companies:
        touched = set(groups)                       # first run, or after a scoring change: score everything on file
    pushes = []
    for key in touched:
        grp = groups.get(key, [])
        if not grp:
            continue
        comp = grp[0]["company"]
        try:
            ctx = {"tier": "unknown"} if args.no_market else market.context(comp["ticker"], comp["cik"])
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            ctx = {"tier": "unknown"}
        sc = score.score_company(grp, ctx, today)
        if not sc:
            continue
        if not comp["ticker"] and ctx.get("ticker"):
            comp = dict(comp, ticker=ctx["ticker"])
        entry = {"ticker": comp["ticker"], "name": comp["name"], "cik": comp["cik"], "updated": today.isoformat(), "ctx": {k: v for k, v in ctx.items() if k != "history"},
                 "spark": ctx.get("history", [])[-90:], "score": sc}
        companies[key] = entry
        # alert? new Strong/Notable, or a clear upgrade since the last alert for this company
        prev = next((a for a in reversed(alerts) if a["key"] == key), None)
        fresh = not prev or (today - date.fromisoformat(prev["date"])).days >= REALERT_GAP_DAYS
        upgraded = prev and (sc["score"] >= prev["score"] + 15 or (sc["label"] == "Strong" and prev["label"] != "Strong"))
        if sc["label"] in ("Strong", "Notable") and (fresh or upgraded):
            alert = {"id": f"{key}-{today.isoformat()}", "key": key, "ticker": comp["ticker"], "name": comp["name"], "cik": comp["cik"],
                     "date": today.isoformat(), "label": sc["label"], "score": sc["score"], "reasons": sc["reasons"], "points": sc["points"],
                     "buyers": sc["buyers"], "n_insiders": sc["n_insiders"], "total_value": sc["total_value"], "avg_price": sc["avg_price"],
                     "first_trade": sc["first_trade"], "last_trade": sc["last_trade"], "take": sc["take"],
                     "ctx": entry["ctx"], "price_at_alert": ctx.get("price"), "perf": {}, "spark": entry["spark"],
                     "filings": sorted({b["adsh"] for b in grp})}
            alerts.append(alert)
            pushes.append(alert)
            log["alerts"].append(f"{comp['ticker']} {sc['label']} {sc['score']}")
    pushes.sort(key=lambda a: -a["score"])
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    for a in [p for p in pushes if p["label"] == "Strong"][:MAX_PUSHES_PER_RUN]:
        try:
            notify.send(notify.alert_text(a["ticker"], a["name"], a, a["ctx"]), repo, token, dry=args.dry)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            log["errors"] += 1

    # 4. how past alerts have done (once a day, cheap) ------------------------------------------------
    cutoff_a = (today - timedelta(days=KEEP_ALERTS_DAYS)).isoformat()
    alerts = [a for a in alerts if a["date"] >= cutoff_a]
    stale = [a for a in alerts if a.get("perf", {}).get("as_of") != today.isoformat()] if not args.no_market else []
    if stale and (not runs or runs[-1]["at"][:10] != today.isoformat() or any(a["date"] == today.isoformat() for a in stale)):
        spy = market.price_history("SPY")
        for a in stale:
            try:
                hist = market.price_history(a["ticker"]) if a["ticker"] else []
                if not hist or not a.get("price_at_alert"):
                    continue
                now = hist[-1][1]
                d0 = date.fromisoformat(a["date"])
                s0, s1 = market.price_on(spy, d0), (spy[-1][1] if spy else None)
                a["perf"] = {"as_of": today.isoformat(), "price": now, "ret": now / a["price_at_alert"] - 1,
                             "spy": (s1 / s0 - 1) if (s0 and s1) else None, "days": (today - d0).days}
            except Exception:  # noqa: BLE001
                continue

    # 5. write ---------------------------------------------------------------------------------------
    seen_list = sorted(seen)[-20000:]
    save("seen.json", seen_list)
    save("buys.json", buys)
    save("alerts.json", alerts)
    save("companies.json", companies)
    log["seconds"] = round(time.time() - t0, 1)
    runs = (runs + [log])[-300:]
    save("runs.json", runs)
    # a small summary the page reads first
    strong = [a for a in alerts if a["label"] == "Strong"]
    perf = [a["perf"] for a in alerts if a.get("perf", {}).get("ret") is not None and a["perf"]["days"] >= 30]
    save("summary.json", {
        "updated": log["at"], "alerts_30d": sum(1 for a in alerts if (today - date.fromisoformat(a["date"])).days <= 30),
        "strong_30d": sum(1 for a in strong if (today - date.fromisoformat(a["date"])).days <= 30),
        "buys_30d": sum(1 for b in buys if (today - date.fromisoformat(b["trade_date"])).days <= 30),
        "companies_tracked": len(companies), "avg_ret": (sum(p["ret"] for p in perf) / len(perf)) if perf else None,
        "avg_vs_spy": (sum(p["ret"] - (p["spy"] or 0) for p in perf) / len(perf)) if perf else None, "n_perf": len(perf),
        "hit_rate": (sum(1 for p in perf if p["ret"] > (p["spy"] or 0)) / len(perf)) if perf else None,
        "last_run": log,
    })
    print(f"done in {log['seconds']}s · alerts: {log['alerts'] or 'none'}")


if __name__ == "__main__":
    main()
