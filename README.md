# Insider

Insider buying that matters — clustered open-market purchases by executives, read straight from SEC filings,
scored, explained, and pushed to your phone.

- `scanner/` — runs every 10 minutes on GitHub Actions: new Form 4s → open-market buys → score → push → `docs/data/*.json`
- `docs/` — the phone app (GitHub Pages). Add it to your iPhone home screen; it receives Web Push directly.

## Setup (once)
1. Settings → Pages → Source: *Deploy from a branch*, branch `main`, folder `/docs`.
2. Secrets (Settings → Secrets and variables → Actions): `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_SUBJECT` (`mailto:you@example.com`).
   Make a key pair with `python scanner/keys.py`.
3. Actions → *scan* → Run workflow (days: 14) to seed it.
4. On the phone: open the Pages URL in Safari → Share → Add to Home Screen → open it → Settings → Turn on notifications → Submit the pre-filled GitHub issue.

Issues labelled `device` hold the push subscriptions; close one to stop alerts to that device.

## Scoring
See the Settings tab in the app, or `scanner/score.py` — every point is explained in the alert itself.
Information, not advice.
