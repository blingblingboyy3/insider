"""Pushing an alert to the phone.

The phone page (added to the home screen) subscribes to Web Push, which gives it a private URL at Apple's
push service. The page hands that subscription to this repo through a one-tap GitHub issue (label `device`);
each run reads those issues and sends the alert to every device. Keys are VAPID: the private half lives in
the repo secret VAPID_PRIVATE_KEY, the public half in docs/config.json."""
import json
import os
import re
import urllib.request

API = "https://api.github.com"


def _gh(path, token, method="GET", body=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "User-Agent": "insider-scanner", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "null")


def subscriptions(repo, token):
    """[(issue_number, subscription dict)] for every open `device` issue, plus any from the PUSH_SUBSCRIPTIONS secret."""
    subs = []
    if repo and token:
        try:
            for issue in _gh(f"/repos/{repo}/issues?labels=device&state=open&per_page=100", token):
                m = re.search(r"\{.*\}", issue.get("body") or "", re.S)
                if not m:
                    continue
                try:
                    s = json.loads(m.group(0))
                except ValueError:
                    continue
                if s.get("endpoint"):
                    subs.append((issue["number"], s))
        except Exception as e:  # noqa: BLE001
            print("could not read device issues:", e)
    extra = os.environ.get("PUSH_SUBSCRIPTIONS", "").strip()
    if extra:
        try:
            for s in json.loads(extra):
                subs.append((None, s))
        except ValueError:
            print("PUSH_SUBSCRIPTIONS is not valid JSON")
    return subs


def private_key():
    """The VAPID private key from the secret, repaired if the paste lost its line breaks or header."""
    raw = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not raw:
        return ""
    body = re.sub(r"-----(BEGIN|END)[A-Z ]*PRIVATE KEY-----", " ", raw)
    body = re.sub(r"\s+", "", body)
    if not body:
        return ""
    import base64
    try:
        der = base64.b64decode(body + "=" * (-len(body) % 4))
    except Exception:  # noqa: BLE001
        return raw
    if len(der) == 32:                                    # a raw 32-byte EC key was pasted instead of a PEM
        return base64.urlsafe_b64encode(der).decode().rstrip("=")
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END PRIVATE KEY-----\n"


def send(alert, repo=None, token=None, dry=False):
    """Push one alert to every device. Returns how many were delivered."""
    key = private_key()
    subs = subscriptions(repo, token)
    if not subs:
        print("no devices subscribed yet")
        return 0
    if dry or not key:
        print(f"[dry] would push to {len(subs)} device(s):", alert["title"])
        return 0
    from pywebpush import WebPushException, webpush
    payload = json.dumps({"title": alert["title"], "body": alert["body"], "url": alert.get("url", "./"), "tag": alert.get("tag", "insider"),
                          "ticker": alert.get("ticker", "")})
    sent = 0
    for num, s in subs:
        try:
            webpush(subscription_info=s, data=payload, vapid_private_key=key, ttl=6 * 3600,
                    vapid_claims={"sub": os.environ.get("VAPID_SUBJECT", "mailto:insider@example.com")})
            sent += 1
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            print(f"push failed ({code}): {e}")
            if code in (404, 410) and num and repo and token:
                # the device unsubscribed or reinstalled: close its issue so we stop trying
                try:
                    _gh(f"/repos/{repo}/issues/{num}", token, "PATCH", {"state": "closed"})
                except Exception:  # noqa: BLE001
                    pass
    return sent


def alert_text(ticker, company, sc, ctx):
    """Title/body for the lock-screen notification: short, with the numbers that matter."""
    buyers = sc["buyers"]
    who = ", ".join(f"{b['role']}" for b in buyers[:3] if not b["fund"]) or "insiders"
    n = sc["n_insiders"]
    head = f"{ticker or company}: {sc['label'].lower()} insider buying"
    lead = f"{n} insider{'s' if n != 1 else ''} ({who}) bought {_money(sc['total_value'])} at ~${sc['avg_price']:.2f}"
    tail = []
    if ctx.get("price"):
        tail.append(f"now ${ctx['price']:.2f}")
    if ctx.get("off_high") is not None:
        tail.append(f"{abs(ctx['off_high']):.0%} off high")
    if ctx.get("market_cap"):
        tail.append(f"{_money(ctx['market_cap'])} cap")
    return {"title": head, "body": lead + (" · " + " · ".join(tail) if tail else "") + f" · score {sc['score']}",
            "tag": f"insider-{ticker}", "ticker": ticker, "url": f"./#{ticker}"}


def _money(v):
    a = abs(v or 0)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if a >= div:
            return f"${v / div:.1f}{unit}".replace(".0", "")
    return f"${v:,.0f}"
