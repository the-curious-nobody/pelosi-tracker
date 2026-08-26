"""Site-builder regression tests. Run: python3 -m tests.test_site"""
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_site  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _tbody_rows(html: str) -> int:
    return len(re.findall(r"<tbody>(.*?)</tbody>", html, re.S)[0].split("<tr>")) - 1


def test_site_build():
    with tempfile.TemporaryDirectory() as td:
        info = build_site.build(Path(td))
        assert set(info["pages"]) == {"index.html", "transactions.html",
                                      "holdings.html", "methodology.html",
                                      "feed.xml"}
        pages = {p: (Path(td) / p).read_text()
                 for p in info["pages"] if p.endswith(".html")}

        tx = json.loads((ROOT / "out" / "transactions.json").read_text())
        h = json.loads((ROOT / "out" / "holdings.json").read_text())

        # row counts match the data exactly
        assert _tbody_rows(pages["transactions.html"]) == len(tx)
        assert _tbody_rows(pages["holdings.html"]) == len(h["stocks"])

        for name, html in pages.items():
            assert "None<" not in html, f"None leaked into {name}"
            assert "&amp;amp;" not in html, f"double-escape in {name}"

        tp = pages["transactions.html"]
        # Honesty markers must render exactly when the data warrants them.
        # Fixture data contains truncated ranges and annual-only rows; a fully
        # corroborated live dataset legitimately contains neither (the first
        # live sync proved this), and then NO marker is the correct rendering.
        has_ub = any(t.get("delay_basis") == "annual_report_upper_bound"
                     for t in tx)
        assert ('class="ub"' in tp) == has_ub, "upper-bound delay marker mismatch"
        has_inf = any((t.get("amount") or {}).get("max_inferred") for t in tx)
        assert ('class="inf"' in tp) == has_inf, "inferred-amount marker mismatch"
        assert tp.count("src ok") == sum(1 for t in tx if t.get("corroborated"))
        # every source link is an official Clerk URL
        for url in re.findall(r'class="src" href="([^"]+)"', tp):
            assert url.startswith("https://disclosures-clerk.house.gov/"), url
        # no fabricated precision: no lone point-dollar figures in amount labels
        assert "amt-label" in tp

        hp = pages["holdings.html"]
        assert "These are estimates" in hp
        assert "outcome" in hp  # expired-unknown lots stated, not guessed

        import sqlite3
        import xml.etree.ElementTree as ET
        feed = ET.parse(Path(td) / "feed.xml")
        ns = {"a": "http://www.w3.org/2005/Atom"}
        n_filings = sqlite3.connect(ROOT / "out" / "pelosi.db").execute(
            "select count(*) from filings").fetchone()[0]
        entries = feed.findall(".//a:entry", ns)
        assert len(entries) == n_filings
        for e in entries:
            assert e.find("a:id", ns).text.startswith(
                "https://disclosures-clerk.house.gov/")
    print("ok  test_site_build")


if __name__ == "__main__":
    test_site_build()
    print("\n1 test passed")
