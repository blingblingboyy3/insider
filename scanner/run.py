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
import re
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
import detail  # noqa: E402
import bigmoney  # noqa: E402

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


KEEP = ("adsh", "filed", "trade_date", "code", "ad", "shares", "price", "value", "owned_after", "direct", "nature", "security", "plan_10b5_1", "company", "owner",
        "derivative", "exercise_price", "expires", "underlying", "underlying_shares")


def compact(b):
    """The fields of a transaction worth keeping around."""
    out = {k: b[k] for k in KEEP if k in b}
    out["notes"] = (b.get("notes") or "")[:300]
    out["owner"] = {k: v for k, v in b["owner"].items() if k != "other_text"}
    return out


def tkey(b):
    return (b["adsh"], b["owner"].get("cik") or b["owner"].get("name"), b["trade_date"], b.get("code"), b["shares"], b.get("security", ""), bool(b.get("derivative")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--days", type=int, default=0, help="how far back to look for filings (default: since last run, min 3)")
    ap.add_argument("--fixtures", help="folder of Form 4 XML files to use instead of EDGAR (tests)")
    ap.add_argument("--no-market", action="store_true", help="skip price/market-cap lookups (tests)")
    ap.add_argument("--rescore", action="store_true", help="re-score every company on file (after a scoring change)")
    ap.add_argument("--backfill", action="store_true", help="fill trade detail and insider histories into existing alerts")
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
    new_buys, new_events = [], []
    if args.fixtures:
        for p in sorted(Path(args.fixtures).glob("*.xml")):
            recs = edgar.parse_form4(p.read_bytes(), adsh=p.stem, filed=today.isoformat())
            new_buys += [r for r in recs if edgar.is_open_market_buy(r)]
            new_events += [r for r in recs if not edgar.is_open_market_buy(r)]
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
            new_events += [r for r in recs if not edgar.is_open_market_buy(r)]
    log["buys"] = len(new_buys)
    print(f"{log['new']} new filings, {len(new_buys)} open-market buys")

    # 2. merge, trim ------------------------------------------------------------------------------
    have = {tkey(b) for b in buys}
    for b in new_buys:
        if tkey(b) not in have:
            buys.append(compact(b))
            have.add(tkey(b))
    cutoff = (today - timedelta(days=KEEP_BUYS_DAYS)).isoformat()
    buys = [b for b in buys if b["trade_date"] >= cutoff]
    # context events: only for companies that have a buy on file (keeps the file small), 120 days
    events = load("events.json", [])
    buy_cos = {b["company"]["cik"] for b in buys}
    have_e = {tkey(e) for e in events}
    for e in new_events:
        if e["company"]["cik"] in buy_cos and tkey(e) not in have_e and e.get("trade_date", "") >= cutoff:
            events.append(compact(e))
            have_e.add(tkey(e))
    events = [e for e in events if e["company"]["cik"] in buy_cos and e["trade_date"] >= cutoff]
    people = load("people.json", {})

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
        sc["buyers"] = detail.buyers(sc["buyers"], grp, events, ctx)
        sc["sells_90d"] = detail.sells(comp["cik"], events, today)
        entry["score"] = sc
        if sc["label"] in ("Strong", "Notable") and (fresh or upgraded):
            if not args.no_market:
                detail.histories(sc["buyers"], comp["cik"], people, today)
            alert = {"id": f"{key}-{today.isoformat()}", "key": key, "ticker": comp["ticker"], "name": comp["name"], "cik": comp["cik"],
                     "date": today.isoformat(), "label": sc["label"], "score": sc["score"], "reasons": sc["reasons"], "points": sc["points"],
                     "buyers": sc["buyers"], "n_insiders": sc["n_insiders"], "total_value": sc["total_value"], "avg_price": sc["avg_price"],
                     "first_trade": sc["first_trade"], "last_trade": sc["last_trade"], "take": sc["take"],
                     "ctx": entry["ctx"], "price_at_alert": ctx.get("price"), "perf": {}, "spark": entry["spark"],
                     "filings": sorted({b["adsh"] for b in grp}), "sells_90d": sc["sells_90d"]}
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

    # 3b. big money: new 13D stakes ------------------------------------------------------------------
    stakes = load("stakes.json", [])
    seen13 = set(load("seen13d.json", []))
    if not args.fixtures:
        try:
            f13 = bigmoney.recent_13d(since, today)
        except Exception as e:  # noqa: BLE001
            print("13D search failed:", e)
            f13 = []
        f13 = [f for f in f13 if f["adsh"] not in seen13][:150]
        shown = False
        for f in f13:
            try:
                doc = bigmoney.filing_doc(f)
                rec = bigmoney.parse_13d(doc, adsh=f["adsh"], filed=f["filed"], form=f.get("form", ""))
            except Exception as e:  # noqa: BLE001
                print("skip 13D", f["adsh"], e)
                log["errors"] += 1
                seen13.add(f["adsh"])
                continue
            seen13.add(f["adsh"])
            # the subject company: the search hit lists it as "Name  (TICK)  (CIK n)"; match on the XML's issuerCIK, else the non-filer name
            parsed = []
            for n in f["names"]:
                m = re.search(r"^(.*?)\s*(?:\(([A-Z.\-]{1,6})\))?\s*\(CIK (\d+)\)", n)
                if m:
                    parsed.append({"name": m.group(1).strip(), "ticker": (m.group(2) or "").upper(), "cik": m.group(3).lstrip("0")})
            filer_cik = f["adsh"][:10].lstrip("0")
            comp = next((x for x in parsed if rec["issuer_cik"] and x["cik"] == rec["issuer_cik"]), None) or \
                   next((x for x in parsed if x["cik"] != filer_cik), None) or {"name": rec["issuer"], "ticker": "", "cik": rec["issuer_cik"]}
            if not shown and rec["source"] == "xml":
                shown = True
                try:
                    import xml.etree.ElementTree as _ET
                    print("[13d sample tags]", sorted({el.tag.split('}')[-1] for el in _ET.fromstring(doc).iter()})[:80])
                except Exception:  # noqa: BLE001
                    pass
            if not rec["filers"]:
                filer = next((x for x in parsed if x["cik"] == filer_cik), None)
                if filer:
                    rec["filers"] = [{"name": filer["name"], "percent": rec["percent"], "shares": rec["shares"], "cik": filer["cik"]}]
            rec["company"] = comp
            prior = next((x["percent"] for x in reversed(stakes) if x["company"]["cik"] == comp["cik"] and x["filers"] and rec["filers"] and x["filers"][0]["name"] == rec["filers"][0]["name"]), None)
            try:
                ctx = {"tier": "unknown"} if args.no_market else market.context(comp["ticker"], comp["cik"])
            except Exception:  # noqa: BLE001
                ctx = {"tier": "unknown"}
            if not comp["ticker"] and ctx.get("ticker"):
                comp["ticker"] = ctx["ticker"]
            sc = bigmoney.score_13d(rec, ctx, prior)
            rec.update({"score": sc, "ctx": {k: v for k, v in ctx.items() if k != "history"}, "spark": ctx.get("history", [])[-90:], "listed_days": len(ctx.get("history") or [])})
            stakes.append(rec)
            lead = sc["lead"]
            norm = lambda n: re.sub(r"[^a-z0-9]", "", (n or "").lower())
            prev13 = next((a for a in reversed(alerts) if a.get("kind") == "13d" and a["cik"] == comp["cik"] and norm(a.get("lead")) == norm(lead)), None)
            fresh13 = not prev13 or (today - date.fromisoformat(prev13["date"])).days >= REALERT_GAP_DAYS or sc["score"] >= prev13["score"] + 15
            if sc["label"] in ("Strong", "Notable") and fresh13:
                alert = {"id": f"13d-{f['adsh']}", "kind": "13d", "key": comp["ticker"] or ("cik" + comp["cik"]), "ticker": comp["ticker"], "name": comp["name"], "cik": comp["cik"],
                         "date": today.isoformat(), "label": sc["label"], "score": sc["score"], "reasons": sc["reasons"], "points": sc["points"], "take": sc["take"],
                         "filers": rec["filers"], "percent": rec["percent"], "shares": rec["shares"], "stake_value": sc["stake_value"], "purpose": rec["purpose"],
                         "form": rec["form"], "amendment": rec["amendment"], "event_date": rec["event_date"], "lead": sc["lead"], "kinds": sc["kinds"],
                         "ctx": rec["ctx"], "price_at_alert": ctx.get("price"), "perf": {}, "spark": rec["spark"], "filings": [f["adsh"]]}
                alerts.append(alert)
                pushes.append(alert)
                log["alerts"].append(f"13D {comp['ticker'] or comp['name'][:12]} {sc['label']} {sc['score']}")
        stakes = [x for x in stakes if x["filed"] >= (today - timedelta(days=KEEP_ALERTS_DAYS)).isoformat()]
        save("stakes.json", stakes)
        save("seen13d.json", sorted(seen13)[-20000:])
        for a in [p for p in pushes if p.get("kind") == "13d" and p["label"] == "Strong"][:MAX_PUSHES_PER_RUN]:
            try:
                notify.send(notify.stake_text(a), repo, token, dry=args.dry)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                log["errors"] += 1

    if args.rescore:
        # re-score stored 13D filings under current rules and rebuild their alerts
        alerts = [a for a in alerts if a.get("kind") != "13d"]
        for rec in sorted(stakes, key=lambda r: r["filed"]):
            comp = rec.get("company") or {}
            ctx = dict(rec.get("ctx") or {}, history=[0] * rec.get("listed_days", 200))
            sc = bigmoney.score_13d(rec, ctx, None)
            rec["score"] = sc
            if sc["label"] in ("Strong", "Notable"):
                norm = lambda n: re.sub(r"[^a-z0-9]", "", (n or "").lower())
                prev13 = next((a for a in reversed(alerts) if a.get("kind") == "13d" and a["cik"] == comp.get("cik") and norm(a.get("lead")) == norm(sc["lead"])), None)
                if prev13 and (date.fromisoformat(rec["filed"]) - date.fromisoformat(prev13["date"])).days < REALERT_GAP_DAYS and sc["score"] < prev13["score"] + 15:
                    continue
                alerts.append({"id": f"13d-{rec['adsh']}", "kind": "13d", "key": comp.get("ticker") or ("cik" + comp.get("cik", "")), "ticker": comp.get("ticker", ""), "name": comp.get("name", ""), "cik": comp.get("cik", ""),
                               "date": rec["filed"], "label": sc["label"], "score": sc["score"], "reasons": sc["reasons"], "points": sc["points"], "take": sc["take"],
                               "filers": rec["filers"], "percent": rec["percent"], "shares": rec["shares"], "stake_value": sc["stake_value"], "purpose": rec["purpose"],
                               "form": rec["form"], "amendment": rec["amendment"], "event_date": rec["event_date"], "lead": sc["lead"], "kinds": sc["kinds"],
                               "ctx": rec["ctx"], "price_at_alert": rec["ctx"].get("price"), "perf": {}, "spark": rec.get("spark", []), "filings": [rec["adsh"]]})
        alerts.sort(key=lambda a: a["date"])
        save("stakes.json", stakes)

    if args.backfill:
        for a in alerts:
            grp = groups.get(a["key"], [])
            if not grp:
                continue
            a["buyers"] = detail.buyers(a["buyers"], grp, events, a.get("ctx", {}))
            a["sells_90d"] = detail.sells(a["cik"], events, today)
            if a["label"] == "Strong" and not args.no_market and (today - date.fromisoformat(a["date"])).days <= 21:
                detail.histories(a["buyers"], a["cik"], people, today)
                save("people.json", people)              # keep progress even if the job is cut short
        for key, entry in companies.items():
            grp = groups.get(key, [])
            if grp:
                entry["score"]["buyers"] = detail.buyers(entry["score"]["buyers"], grp, events, entry.get("ctx", {}))
                entry["score"]["sells_90d"] = detail.sells(entry["cik"], events, today)

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
    save("events.json", events)
    save("people.json", {k: v for k, v in people.items() if v.get("as_of", "") >= (today - timedelta(days=60)).isoformat()})
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
        "stakes_30d": sum(1 for a in alerts if a.get("kind") == "13d" and (today - date.fromisoformat(a["date"])).days <= 30),
        "companies_tracked": len(companies), "avg_ret": (sum(p["ret"] for p in perf) / len(perf)) if perf else None,
        "avg_vs_spy": (sum(p["ret"] - (p["spy"] or 0) for p in perf) / len(perf)) if perf else None, "n_perf": len(perf),
        "hit_rate": (sum(1 for p in perf if p["ret"] > (p["spy"] or 0)) / len(perf)) if perf else None,
        "last_run": log,
    })
    print(f"done in {log['seconds']}s · alerts: {log['alerts'] or 'none'}")


if __name__ == "__main__":
    main()
