"""Big money: Schedule 13D filings — someone crossing 5% of a company with intent to influence it.

Since late 2024 the SEC requires 13D/13G in structured XML, so the stake, the filer and the stated purpose
can be read directly. Older-style text filings are parsed by pattern as a fallback. 13G (passive holders:
index funds, asset managers) is read too but never alerted; it is kept so a 13G→13D switch shows up."""
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date

from edgar import ARCHIVE, SEARCH, get

PASSIVE = r"\b(vanguard|blackrock|state street|fidelity|fmr|geode|invesco|northern trust|dimensional|t\. ?rowe|capital research|wellington|jpmorgan chase|bank of america|morgan stanley|goldman sachs asset|ubs group|allianz|schwab|nuveen|franklin|principal|prudential|janus|macquarie|amundi|norges|susquehanna|citadel advisors|millennium|two sigma|renaissance|d\. ?e\. ?shaw|jane street|point72)\b"
ACTIVISTS = r"\b(elliott|starboard|icahn|third point|pershing|trian|jana|engaged capital|ancora|land & buildings|sachem head|corvex|value ?act|valueact|legion partners|mantle ridge|politan|irenic|browning west|impactive|saba|glenview|hudson bay|engine no|cevian|palliser|anson|lion point|scion|kimmeridge|bluescape|hg vora|oasis management|silver point|carl c\. icahn|blackwells|barington|voss capital|privet|cannell|driver management)\b"


def recent_13d(since, until=None, max_pages=10):
    """New 13D filings (originals and amendments). The structured form is called 'SCHEDULE 13D' on EDGAR; the
    legacy text form 'SC 13D' is still used by a few filers, so both are searched."""
    until = until or date.today()
    out, seen = [], set()
    for forms in ("SCHEDULE 13D", "SC 13D"):
        for page in range(max_pages):
            q = {"forms": forms, "dateRange": "custom", "startdt": since.isoformat(), "enddt": until.isoformat(), "from": page * 100}
            data = json.loads(get(SEARCH + "?" + urllib.parse.urlencode(q)).decode())
            hits = data.get("hits", {}).get("hits", [])
            for h in hits:
                src = h.get("_source", {})
                adsh, _, fname = (h.get("_id") or "").partition(":")
                if not adsh or adsh in seen:
                    continue
                seen.add(adsh)
                ciks = [str(c).lstrip("0") for c in (src.get("ciks") or [])]
                out.append({"adsh": adsh, "cik": ciks[0] if ciks else adsh[:10].lstrip("0"), "ciks": ciks, "filed": src.get("file_date", ""), "file": fname,
                            "form": src.get("file_type") or src.get("form_type") or forms, "names": src.get("display_names") or []})
            if len(hits) < 100:
                break
    return out


def _iso(d):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", d or "")
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else (d or "")[:10]


def _local(tag):
    return tag.split("}")[-1]


def _walk(root):
    for el in root.iter():
        yield _local(el.tag), (el.text or "").strip(), el


def _num(s):
    try:
        return float(re.sub(r"[^0-9.\-]", "", s or "") or 0)
    except ValueError:
        return 0.0


def parse_13d(xml_bytes, adsh="", filed="", form=""):
    """One 13D as a plain record. Works on the SEC's structured XML; falls back to text patterns."""
    text = xml_bytes.decode("utf-8", "replace")
    rec = {"adsh": adsh, "filed": filed, "form": form or ("SC 13D/A" if "/A" in text[:4000] else "SC 13D"), "issuer": "", "issuer_cik": "", "cusip": "",
           "filers": [], "percent": 0.0, "shares": 0.0, "purpose": "", "event_date": "", "amendment": "/A" in (form or ""), "source": ""}
    try:
        root = ET.fromstring(xml_bytes)
        rec["source"] = "xml"
        filers, cur = [], None
        for name, val, el in _walk(root):
            n = name.lower()
            if n in ("issuername", "nameofissuer") and val:
                rec["issuer"] = rec["issuer"] or val
            elif n in ("issuercik",) and val:
                rec["issuer_cik"] = val.lstrip("0")
            elif n == "submissiontype" and val:
                rec["form"] = val
                rec["amendment"] = "/A" in val
            elif n == "amendmentno" and val:
                rec["amendment"] = True
                rec["amendment_no"] = val
            elif n in ("issuercusip", "cusip", "cusipnumber") and val:
                rec["cusip"] = rec["cusip"] or val
            elif n in ("dateofevent", "eventdaterequiresfilingthisstatement", "dateofeventwhichrequiresfilingofthisstatement") and val:
                rec["event_date"] = _iso(val)
            elif n in ("reportingpersonname", "nameofreportingperson", "rptownername"):
                if val:
                    cur = {"name": val, "percent": 0.0, "shares": 0.0, "cik": ""}
                    filers.append(cur)
            elif n in ("reportingpersoncik", "filercik", "rptownercik") and val and cur is not None and not cur["cik"]:
                cur["cik"] = val.lstrip("0")
            elif n in ("percentofclass", "percentofclassrepresented", "percentofclassrepresentedbyamount") and val:
                v = _num(val)
                if cur is not None and not cur["percent"]:
                    cur["percent"] = v
                rec["percent"] = max(rec["percent"], v)
            elif n in ("aggregateamountowned", "aggregateamountbeneficiallyowned", "aggregateamountbeneficiallyownedbyeachreportingperson") and val:
                v = _num(val)
                if cur is not None and not cur["shares"]:
                    cur["shares"] = v
                rec["shares"] = max(rec["shares"], v)
            elif n in ("transactionpurpose", "purposeoftransaction", "item4", "purposeoftransactiondescription") and val and not rec["purpose"]:
                rec["purpose"] = re.sub(r"\s+", " ", "".join(el.itertext())).strip()[:1500]
        rec["filers"] = [f for f in filers if f["name"]]
    except ET.ParseError:
        rec["source"] = "text"
        plain = re.sub(r"<[^>]+>", " ", text)
        plain = re.sub(r"\s+", " ", plain)
        m = re.search(r"(?:Act of 1934\)?|Amendment No\. ?\d+\)?\*?)\s+([A-Z][A-Za-z0-9&.,' \-]{3,80}?)\s*\(Name of Issuer\)", plain) or \
            re.search(r"Name of Issuer\)?\s*[:\-]?\s*([A-Z][A-Za-z0-9&.,' \-]{3,80}?)\s+\(?(?:Title of Class|Common)", plain)
        if m:
            rec["issuer"] = m.group(1).strip()
        m = re.search(r"\b([0-9A-Z]{9})\b\s*\(CUSIP", plain) or re.search(r"CUSIP (?:Number|No\.?)[^0-9A-Z]{0,20}([0-9A-Z]{9})", plain)
        if m:
            rec["cusip"] = m.group(1)
        pcts = [float(x) for x in re.findall(r"Percent of Class Represented.{0,80}?(\d{1,2}(?:\.\d+)?)\s*%", plain, re.I)]
        rec["percent"] = max(pcts) if pcts else 0.0
        m = re.search(r"Item 4\.?\s*Purpose of Transaction\.?\s*(.{40,1500}?)(?:Item 5\.|$)", plain, re.I)
        if m:
            rec["purpose"] = m.group(1).strip()
        names = re.findall(r"Names? of Reporting Persons?\)?\s*[:\-]?\s*(?:I\.R\.S\.[^A-Z]{0,40})?([A-Z][A-Za-z0-9&.,' \-]{3,70}?)\s*(?:\(?\b2\b\)?\s*)?(?:Check the Appropriate|\(?[bB]\))", plain)
        names = [re.sub(r"\s+\d+$", "", n).strip() for n in names]
        rec["filers"] = [{"name": n, "percent": rec["percent"], "shares": 0.0, "cik": ""} for n in dict.fromkeys(names) if len(n) > 3][:6]
        m = re.search(r"Aggregate Amount Beneficially Owned.{0,60}?([\d,]{4,})", plain, re.I)
        if m:
            rec["shares"] = _num(m.group(1))
    return rec


def filing_doc(f):
    """The primary document of a 13D: the XML when present, else the main .htm/.txt."""
    folder = f"{ARCHIVE}/{f['cik']}/{f['adsh'].replace('-', '')}"
    if f.get("file") and f["file"].lower().endswith(".xml"):
        return get(f"{folder}/{f['file']}")
    idx = get(folder + "/").decode("utf-8", "replace")
    names = re.findall(r'href="[^"]*?/([^"/]+\.(?:xml|htm|html|txt))"', idx, re.I)
    names = [n for n in names if not n.lower().startswith(("xslf", "filingsummary", "0001", "r1", "r2", "financial"))]
    xmls = [n for n in names if n.lower().endswith(".xml") and "primary_doc" in n.lower()] or [n for n in names if n.lower().endswith(".xml")]
    pick = (xmls or names)[0]
    return get(f"{folder}/{pick}")


def filer_kind(name):
    n = name.lower()
    if re.search(ACTIVISTS, n):
        return "activist"
    if re.search(PASSIVE, n):
        return "passive"
    if re.search(r"\b(l\.?p\.?|llc|capital|partners|management|advisors|fund|holdings|investments?|ventures|asset)\b", n):
        return "fund"
    return "person"


def score_13d(rec, ctx, prior=None):
    """0–100 with reasons. Strongest: a known activist or an individual/fund taking a fresh 5%+ stake with an activist
    purpose, in a company small enough for a stake to matter, bought on the open market."""
    pts, reasons = {}, []
    filers = rec["filers"] or [{"name": n, "percent": rec["percent"]} for n in []]
    kinds = [filer_kind(f["name"]) for f in filers] or ["unknown"]
    lead = filers[0]["name"] if filers else "An investor"
    pts["who"] = 35 if "activist" in kinds else 20 if "fund" in kinds else 15 if "person" in kinds else 0
    if "activist" in kinds:
        reasons.append(f"{lead} is a known activist — their campaigns historically move stocks and force outcomes")
    elif "passive" in kinds:
        reasons.append("A passive asset manager; 13D filings from them are usually technical")
    pct = rec["percent"]
    pts["stake"] = 20 if pct >= 15 else 15 if pct >= 10 else 10 if pct >= 7 else 6 if pct >= 5 else 0
    if pct:
        reasons.append(f"Owns {pct:.1f}% of the company")
    pts["fresh"] = 0 if rec["amendment"] else 15
    if rec["amendment"]:
        if prior and pct > prior + 1:
            pts["fresh"] = 10
            reasons.append(f"Raising the stake: {prior:.1f}% → {pct:.1f}%")
        else:
            reasons.append("An amendment — an update to a stake already disclosed")
    else:
        reasons.append("A new 13D: the first time this holder has crossed 5% with intent")
    purpose = (rec.get("purpose") or "").lower()
    pts["purpose"] = 0
    if re.search(r"board|director|nominat|proxy|strategic alternatives|sale of the (company|issuer)|merger|acquire|take private|engage|discuss.*(management|board)|unlock|maximiz", purpose):
        pts["purpose"] += 15
        reasons.append("Stated purpose mentions the board, a sale, or engaging management — this is an active campaign, not a passive holding")
    elif re.search(r"investment purposes|ordinary course", purpose):
        reasons.append("Stated purpose is 'investment purposes' — may stay passive")
    # how the stake was acquired matters more than anything: bought on the market = conviction; received in a deal = plumbing
    pts["how"] = 0
    deal = re.search(r"in connection with|pursuant to (?:the|a) (?:merger|business combination|initial public offering|ipo|agreement and plan|securities purchase|subscription|exchange|conversion|reorgani[sz]ation|underwriting|private placement|registered direct)|as (?:compensation|consideration)|founder|restricted shares|upon (?:the )?(?:closing|consummation|conversion|exercise)|preferred (?:stock|shares)|warrants?|convertible|underwriter|placement agent|de-?spac|spac", purpose)
    bought = re.search(r"open[- ]market|purchased (?:the )?(?:shares|common)|acquired .{0,40}(?:in the open market|through (?:open[- ]market|brokerage))|undervalued|attractive investment|believe", purpose)
    if deal and not bought:
        pts["how"] = -30
        reasons.append("Shares came from a deal (IPO, merger, financing, compensation or conversion) rather than open-market buying — this is paperwork, not a bet")
    elif bought:
        pts["how"] = 8
        reasons.append("Bought on the open market, by the filer's own account")
    fund_target = re.search(r"\b(fund|trust|municipal|income|closed[- ]end|portfolio|strategic opportunities|term trust)\b", (rec.get("issuer") or "").lower() + " " + ((rec.get("company") or {}).get("name") or "").lower())
    if fund_target:
        pts["how"] -= 10
        reasons.append("The target is a closed-end fund or trust — discount-arbitrage activism, a different game from operating companies")
    pts["cap"] = {"micro": 4, "small": 10, "mid": 10, "large": 6, "mega": 2}.get(ctx.get("tier", "unknown"), 4)
    if ctx.get("tier") in ("small", "mid"):
        reasons.append(f"{ctx['tier'].title()}-cap ({_money(ctx.get('market_cap'))}): a 5%+ holder has real leverage here")
    value = (ctx.get("market_cap") or 0) * pct / 100 if ctx.get("market_cap") else 0
    score = max(0, min(100, sum(pts.values())))
    label = "Strong" if score >= 70 else "Notable" if score >= 45 else "Weak"
    if "passive" in kinds and "activist" not in kinds:
        label = "Weak"
    if pct >= 30 and "activist" not in kinds:
        label = "Weak"
        reasons.append(f"A {pct:.0f}% holder is control, not activism — routine disclosure")
    if not ctx.get("price") or (ctx.get("market_cap") or 0) < 50e6:
        label = "Weak" if label != "Weak" else label
        reasons.append("Too small or not actively traded to act on" if ctx.get("price") else "No listed price found for the target")
    if len(ctx.get("history") or []) and len(ctx["history"]) < 90 and "activist" not in kinds:
        label = "Weak"
        reasons.append("Recent listing — early 13Ds are founders and IPO investors reporting initial stakes")
    if rec["amendment"] and prior is not None and pct <= prior + 0.5 and "activist" not in kinds:
        label = "Weak" if label == "Notable" else label
        reasons.append("Stake unchanged or lower than last filing")
    take = {"Strong": ("An activist-style stake. Historically the stock gains several percent on the disclosure and the holder pushes for change over the following 6–18 months — a board seat, a sale, buybacks. "
                       "People who follow these treat the filer's track record as the main input and expect a long, noisy path."),
            "Notable": "A meaningful 5%+ holder with intent, but without the hallmarks of a full campaign yet. Worth watching for amendments that raise the stake or name directors.",
            "Weak": "A technical or passive filing; logged for context."}[label]
    return {"score": score, "label": label, "points": pts, "reasons": reasons, "kinds": kinds, "lead": lead, "stake_value": round(value, 2),
            "take": {"headline": f"{lead} · {pct:.1f}% stake", "body": take, "horizon": "6–18 months"}}


def _money(v):
    if not v:
        return "—"
    a = abs(v)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if a >= div:
            return f"${v / div:.1f}{unit}".replace(".0", "")
    return f"${v:,.0f}"
