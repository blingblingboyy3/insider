"""Reading insider filings straight from the SEC.

Every insider trade at a US public company must be reported on Form 4 within two business days, and the SEC
publishes each one as structured XML on EDGAR for free. This module finds the newest filings and turns each
into plain records: who, what role, bought or sold, how many shares, at what price, what they owned afterward.

The SEC asks for a descriptive User-Agent with a contact and no more than ~10 requests a second. We stay well
under that.
"""
import json
import os
import re
import time
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# The SEC requires every automated caller to identify itself with a name and a contact address, and it answers
# 403 to anything that doesn't. Set SEC_CONTACT to your email in the repo secrets to use your own.
UA = os.environ.get("SEC_USER_AGENT") or f"Insider personal insider-buying scanner {os.environ.get('SEC_CONTACT') or 'set SEC_CONTACT to your email'}"
SEARCH = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"
CURRENT = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&output=atom&count=100&start={start}"

_LAST = [0.0]


def _pace(min_gap=0.12):
    wait = _LAST[0] + min_gap - time.time()
    if wait > 0:
        time.sleep(wait)
    _LAST[0] = time.time()


def get(url, tries=3, timeout=30):
    """One polite request to the SEC (bytes). Retries on transient errors."""
    last = None
    for i in range(tries):
        _pace()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate", "Accept": "application/json, text/html, */*",
                                                       "Host": urllib.parse.urlparse(url).netloc})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            body = ""
            try:
                raw = e.read(4000)
                if e.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                body = re.sub(r"<[^>]+>|\s+", " ", raw.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                pass
            last = f"{e} :: {body[:400]}"
            if e.code in (403, 404):
                break
            time.sleep(1.5 * (i + 1))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"SEC request failed: {url} ({last})")


# ------------------------------------------------------------------ finding new filings

def recent_filings(since, until=None, max_pages=20):
    """Form 4 filings filed between `since` and `until` (dates), newest first, from EDGAR full-text search.
    Each: {"adsh", "cik", "filed", "file", "names"}."""
    until = until or datetime.now(timezone.utc).date()
    out, seen = [], set()
    for page in range(max_pages):
        q = {"forms": "4", "dateRange": "custom", "startdt": since.isoformat(), "enddt": until.isoformat(), "from": page * 100}
        data = json.loads(get(SEARCH + "?" + urllib.parse.urlencode(q)).decode())
        hits = data.get("hits", {}).get("hits", [])
        for h in hits:
            src = h.get("_source", {})
            adsh, _, fname = (h.get("_id") or "").partition(":")
            if not adsh or adsh in seen or not fname.lower().endswith(".xml"):
                continue
            seen.add(adsh)
            ciks = src.get("ciks") or []
            out.append({"adsh": adsh, "cik": str(ciks[0]).lstrip("0") if ciks else "", "ciks": [str(c).lstrip("0") for c in ciks],
                        "filed": src.get("file_date", ""), "file": fname, "names": src.get("display_names") or []})
        if len(hits) < 100:
            break
    return out


def current_filings(pages=3):
    """Fallback: the SEC's 'latest filings' feed — the last few hundred Form 4s with links to their folders."""
    out = []
    for p in range(pages):
        xml = get(CURRENT.format(start=p * 100)).decode("utf-8", "replace")
        for m in re.finditer(r"<entry>(.*?)</entry>", xml, re.S):
            e = m.group(1)
            link = re.search(r'href="([^"]+)"', e)
            title = re.search(r"<title>(.*?)</title>", e, re.S)
            upd = re.search(r"<updated>(.*?)</updated>", e)
            if not link:
                continue
            mm = re.search(r"/data/(\d+)/(\d{18})/", link.group(1))
            if not mm:
                continue
            cik, raw = mm.group(1), mm.group(2)
            adsh = f"{raw[:10]}-{raw[10:12]}-{raw[12:]}"
            out.append({"adsh": adsh, "cik": cik, "ciks": [cik], "filed": (upd.group(1) if upd else "")[:10], "file": "",
                        "names": [re.sub(r"\s+", " ", title.group(1)).strip()] if title else [], "index": link.group(1)})
    return out


def filing_xml(f):
    """The Form 4 XML document of one filing (bytes)."""
    folder = f"{ARCHIVE}/{f['cik']}/{f['adsh'].replace('-', '')}"
    if f.get("file"):
        return get(f"{folder}/{f['file']}")
    idx = get(folder + "/").decode("utf-8", "replace")
    names = re.findall(r'href="[^"]*?/([^"/]+\.xml)"', idx)
    names = [n for n in names if not n.lower().startswith(("primary_doc", "xslf345", "filingsummary"))] or names
    if not names:
        raise RuntimeError("no xml in filing folder")
    return get(f"{folder}/{names[0]}")


# ------------------------------------------------------------------ reading one Form 4

def _t(node, path, default=""):
    el = node.find(path) if node is not None else None
    return (el.text or "").strip() if el is not None and el.text else default


def _num(s, default=0.0):
    try:
        return float(str(s).replace(",", "").replace("$", "") or default)
    except ValueError:
        return default


def parse_form4(xml_bytes, adsh="", filed=""):
    """Every transaction in a Form 4 as a list of plain records (one per owner × transaction)."""
    root = ET.fromstring(xml_bytes)
    if root.tag != "ownershipDocument":
        return []
    issuer = root.find("issuer")
    ticker = _t(issuer, "issuerTradingSymbol").upper().strip()
    if ticker in ("N/A", "NA", "NONE", "N.A.", "-", "--") or len(ticker) > 8:
        ticker = ""                                         # the filer left it blank; the SEC's company record fills it in later
    company = {"cik": _t(issuer, "issuerCik").lstrip("0"), "name": _t(issuer, "issuerName"), "ticker": ticker}
    foot = {f.get("id"): re.sub(r"\s+", " ", "".join(f.itertext())).strip() for f in root.iter("footnote")}
    owners = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        title = _t(rel, "officerTitle")
        owners.append({"cik": _t(ro, "reportingOwnerId/rptOwnerCik").lstrip("0"), "name": _t(ro, "reportingOwnerId/rptOwnerName"),
                       "director": _t(rel, "isDirector") in ("1", "true"), "officer": _t(rel, "isOfficer") in ("1", "true"),
                       "ten_percent": _t(rel, "isTenPercentOwner") in ("1", "true"), "other": _t(rel, "isOther") in ("1", "true"),
                       "title": title, "other_text": _t(rel, "otherText")})
    period = _t(root, "periodOfReport")
    recs = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        notes = " ".join(foot.get(fid.get("id"), "") for fid in tx.iter("footnoteId"))
        shares = _num(_t(tx, "transactionAmounts/transactionShares/value"))
        price = _num(_t(tx, "transactionAmounts/transactionPricePerShare/value"))
        rec = {
            "adsh": adsh, "filed": filed or period, "trade_date": _t(tx, "transactionDate/value") or period,
            "security": _t(tx, "securityTitle/value"), "code": _t(tx, "transactionCoding/transactionCode").upper(),
            "form_type": _t(tx, "transactionCoding/transactionFormType"),
            "ad": _t(tx, "transactionAmounts/transactionAcquiredDisposedCode/value").upper(),
            "shares": shares, "price": price, "value": round(shares * price, 2),
            "owned_after": _num(_t(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")),
            "direct": _t(tx, "ownershipNature/directOrIndirectOwnership/value").upper() != "I",
            "nature": _t(tx, "ownershipNature/natureOfOwnership/value"),
            "notes": notes, "plan_10b5_1": bool(re.search(r"10b5-1", notes, re.I)),
            "company": company,
        }
        for o in owners:
            recs.append(dict(rec, owner=o))
    # derivatives: options, warrants, RSUs — exercises, grants, conversions. Context, never a buy signal.
    for tx in root.findall("derivativeTable/derivativeTransaction"):
        notes = " ".join(foot.get(fid.get("id"), "") for fid in tx.iter("footnoteId"))
        shares = _num(_t(tx, "transactionAmounts/transactionShares/value"))
        price = _num(_t(tx, "transactionAmounts/transactionPricePerShare/value"))
        rec = {
            "adsh": adsh, "filed": filed or period, "trade_date": _t(tx, "transactionDate/value") or period, "derivative": True,
            "security": _t(tx, "securityTitle/value"), "code": _t(tx, "transactionCoding/transactionCode").upper(),
            "form_type": _t(tx, "transactionCoding/transactionFormType"),
            "ad": _t(tx, "transactionAmounts/transactionAcquiredDisposedCode/value").upper(),
            "shares": shares, "price": price, "value": round(shares * price, 2),
            "exercise_price": _num(_t(tx, "conversionOrExercisePrice/value")),
            "exercisable": _t(tx, "exerciseDate/value"), "expires": _t(tx, "expirationDate/value"),
            "underlying": _t(tx, "underlyingSecurity/underlyingSecurityTitle/value"),
            "underlying_shares": _num(_t(tx, "underlyingSecurity/underlyingSecurityShares/value")),
            "owned_after": _num(_t(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")),
            "direct": _t(tx, "ownershipNature/directOrIndirectOwnership/value").upper() != "I",
            "nature": _t(tx, "ownershipNature/natureOfOwnership/value"),
            "notes": notes, "plan_10b5_1": bool(re.search(r"10b5-1", notes, re.I)),
            "company": company,
        }
        for o in owners:
            recs.append(dict(rec, owner=o))
    return recs


CODES = {"P": "Open-market purchase", "S": "Open-market sale", "A": "Grant / award", "M": "Option exercise", "F": "Tax withholding",
         "G": "Gift", "D": "Sale back to company", "C": "Conversion", "X": "Option exercise (in-the-money)", "J": "Other (see note)",
         "W": "Will / inheritance", "I": "Discretionary (plan)", "Z": "Trust deposit/withdrawal", "K": "Equity swap", "U": "Tender of shares",
         "L": "Small acquisition", "H": "Expired long", "E": "Expired short", "O": "Out-of-the-money exercise"}


def describe(rec):
    """A short label for one transaction, the way a person would say it."""
    base = CODES.get(rec.get("code", ""), "Transaction " + rec.get("code", "?"))
    if rec.get("derivative"):
        sec = rec.get("security") or "derivative"
        if rec.get("code") == "A":
            return f"Granted {sec}"
        if rec.get("code") in ("M", "X"):
            return f"Exercised {sec}"
        return f"{base} — {sec}"
    if rec.get("plan_10b5_1") and rec.get("code") == "P":
        return "Purchase under a 10b5-1 plan"
    return base


def insider_filings(owner_cik, issuer_cik, limit=12):
    """Recent Form 4s this insider filed about this company (accession numbers, newest first), from their SEC record."""
    try:
        data = json.loads(get(f"https://data.sec.gov/submissions/CIK{int(owner_cik):010d}.json").decode())
    except Exception:  # noqa: BLE001
        return []
    rec = data.get("filings", {}).get("recent", {})
    out = []
    for form, adsh, date in zip(rec.get("form", []), rec.get("accessionNumber", []), rec.get("filingDate", [])):
        if form in ("4", "4/A"):
            out.append({"adsh": adsh, "filed": date})
        if len(out) >= limit:
            break
    return out


def insider_history(owner_cik, issuer_cik, months=12, limit=10):
    """This insider's own transactions at this company over the past year, parsed from their filings."""
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=30 * months)).isoformat()
    recs = []
    for f in insider_filings(owner_cik, issuer_cik, limit):
        if f["filed"] < cutoff:
            break
        try:
            xml = filing_xml({"cik": owner_cik, "adsh": f["adsh"], "file": ""})
            recs += [r for r in parse_form4(xml, adsh=f["adsh"], filed=f["filed"])
                     if r["company"]["cik"] == str(issuer_cik).lstrip("0") and (r["owner"].get("cik") or "").lstrip("0") == str(owner_cik).lstrip("0")]
        except Exception:  # noqa: BLE001
            continue
    return recs


def role_of(owner):
    """A short, readable role: CEO, CFO, Chair, President, COO, Officer (title), Director, 10% owner."""
    t = (owner.get("title") or "").lower()
    if owner.get("officer") or t:
        for pat, lab in ((r"chief exec|\bceo\b", "CEO"), (r"chief fin|\bcfo\b", "CFO"), (r"chief oper|\bcoo\b", "COO"),
                         (r"president", "President"), (r"chair", "Chair"), (r"chief", "C-suite"), (r"general counsel|legal", "General Counsel"),
                         (r"\bevp\b|executive vice|\bsvp\b|senior vice|vice president|\bvp\b", "VP")):
            if re.search(pat, t):
                return lab
        return "Officer"
    if owner.get("director"):
        return "Director"
    if owner.get("ten_percent"):
        return "10% owner"
    return "Insider"


def is_open_market_buy(rec):
    return rec["code"] == "P" and rec["ad"] == "A" and rec["shares"] > 0 and rec["price"] > 0


def is_open_market_sale(rec):
    return rec["code"] == "S" and rec["ad"] == "D" and rec["shares"] > 0 and rec["price"] > 0


def looks_like_fund(name):
    return bool(re.search(r"\b(l\.?p\.?|llc|l\.l\.c\.|fund|capital|partners|holdings|management|advisors|trust|ventures|investments?|group inc)\b", name.lower()))


def window(days):
    today = datetime.now(timezone.utc).date()
    return today - timedelta(days=days), today
