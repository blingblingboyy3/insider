"""python -m pytest tests  (or just python tests/test_scanner.py)"""
import json, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scanner"))
import edgar, score  # noqa: E402

def test_parse_and_filter():
    recs = edgar.parse_form4((ROOT / "tests/fixtures/0001-26-000007.xml").read_bytes(), adsh="x")
    assert len(recs) == 2 and not any(edgar.is_open_market_buy(r) for r in recs)
    assert edgar.role_of(recs[0]["owner"]) == "CEO"

def test_cluster_scores_strong_and_fund_scores_weak():
    buys = []
    for p in sorted((ROOT / "tests/fixtures").glob("*.xml")):
        buys += [r for r in edgar.parse_form4(p.read_bytes(), adsh=p.stem) if edgar.is_open_market_buy(r)]
    g = score.group_buys(buys)
    assert score.score_company(g["HRBR"], {"tier": "small"})["label"] == "Strong"
    assert score.score_company(g["TNYR"], {"tier": "micro"})["label"] == "Weak"
    assert score.score_company(g["PLNC"], {"tier": "mid"})["points"]["penalty"] == -10

def test_run_offline(tmp_path):
    env = {"PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, str(ROOT / "scanner/run.py"), "--fixtures", str(ROOT / "tests/fixtures"), "--no-market", "--dry"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    alerts = json.loads((ROOT / "docs/data/alerts.json").read_text())
    assert any(a["ticker"] == "HRBR" and a["label"] == "Strong" for a in alerts)

if __name__ == "__main__":
    test_parse_and_filter(); test_cluster_scores_strong_and_fund_scores_weak(); test_run_offline(None); print("all good")
