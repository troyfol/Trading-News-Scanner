"""Ground-truth provenance dump for BBCP earnings (scrape vs parquet).
Run with the repo venv python. Read-only; no writes anywhere.

Dev-only: the live finviz/SEC scraping + parquet read run inside main()
under an ``if __name__ == "__main__"`` guard, so merely IMPORTING this
module never fires network traffic (it previously ran at module level,
hammering finviz with a spoofed UA on any import)."""
import json
import re
import time
import pandas as pd

import scan_sec as ss

SYM = "BBCP"


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def main():
    PARQUET = ss.DEFAULT_EARNINGS_DB_PATH

    pd.set_option("display.width", 240)
    pd.set_option("display.max_columns", 40)

    # ------------------------------------------------------------ PARQUET
    hr("1) PARQUET rows for %s  (%s)" % (SYM, PARQUET))
    df = pd.read_parquet(PARQUET)
    sub = df[df["ticker"] == SYM].copy()
    sub = sub.sort_values("period_ending")
    cols = [c for c in ["period_ending", "report_date", "report_time", "source",
                        "report_date_proxy", "estimated_eps", "reported_eps",
                        "surprise_eps_pct", "yoy_eps_pct", "estimated_rev",
                        "reported_rev", "surprise_rev_pct", "yoy_rev_pct"]
            if c in sub.columns]
    print("rows:", len(sub))
    with pd.option_context("display.float_format", lambda v: f"{v:,.4f}"):
        print(sub[cols].tail(12).to_string(index=False))
    print("\nnewest parquet report_date:", sub["report_date"].max())

    # --------------------------------------------------------- SNAPSHOT (p=d)
    fetcher = ss.DataFetcher()
    fetcher.finviz_min_interval = 1.0

    hr("2) LIVE quote.ashx?p=d  scrape_finviz  -> meta")
    meta, _items = fetcher.scrape_finviz(SYM)
    for k in ("name", "earnings", "eps_surprise", "sales_surprise"):
        print(f"  meta[{k!r}] = {meta.get(k)!r}")
    print("  finviz_status:", fetcher.finviz_status)

    # raw EPS/Sales Surpr cell so we can see the exact HTML formatting
    hr("2b) RAW 'EPS/Sales Surpr.' cell from p=d")
    try:
        from bs4 import BeautifulSoup
        r = fetcher.session.get(f"https://finviz.com/quote.ashx?t={SYM}&p=d",
                                headers=ss.BROWSER_HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        # Finviz's snapshot grid is now 6 separate snapshot-table2 columns —
        # walk them all (find() saw only the first, missing Earnings/EPS-Sales).
        for snap in soup.find_all("table", class_="snapshot-table2"):
            for tr in snap.find_all("tr"):
                tds = tr.find_all("td")
                for i in range(0, len(tds) - 1, 2):
                    label = tds[i].get_text(strip=True).lower()
                    if "eps/sales" in label and "surpr" in label:
                        cell = tds[i + 1]
                        kids = [c for c in cell.children if hasattr(c, "get_text")]
                        print("  cell text:", repr(cell.get_text(strip=True)))
                        print("  cell html:", repr(str(cell))[:300])
                        print("  child spans:", [c.get_text(strip=True) for c in kids])
                    if label == "earnings":
                        print("  earnings cell:", repr(tds[i + 1].get_text(strip=True)))
    except Exception as e:
        print("  raw cell dump failed:", type(e).__name__, e)

    time.sleep(1.5)

    # ------------------------------------------------------------ ty=ea
    hr("3) LIVE quote.ashx?ty=ea  raw earningsData entries (most recent 8)")
    r2 = fetcher.session.get(f"https://finviz.com/quote.ashx?t={SYM}&ty=ea",
                             headers=ss.BROWSER_HEADERS, timeout=10)
    entries = ss._fv_ea_extract(r2.text or "")
    if entries is None:
        print("  earningsData NOT FOUND (status %s)" % r2.status_code)
        entries = []
    print("  entries:", len(entries))
    keys = ["earningsDate", "fiscalEndDate", "epsEstimate", "epsActual",
            "epsReported", "salesEstimate", "salesActual", "salesReported"]
    for e in entries[-8:]:
        print("  " + " | ".join(f"{k}={e.get(k)}" for k in keys if k in e))

    hr("3b) ty=ea -> canonical rows via _fv_ea_rows_with_yoy (most recent 8)")
    rows = ss._fv_ea_rows_with_yoy(entries, SYM)
    crows = ["period_ending", "report_date", "estimated_eps", "reported_eps",
             "surprise_eps_pct", "yoy_eps_pct", "estimated_rev", "reported_rev",
             "surprise_rev_pct", "yoy_rev_pct"]
    for r in rows[-8:]:
        print("  " + " | ".join(
            f"{k}={r.get(k):.4f}" if isinstance(r.get(k), float) and r.get(k) == r.get(k)
            else f"{k}={r.get(k)}"
            for k in crows))

    # ------------------------------------------------------------ LANDING SIM
    hr("4) What the LANDING ROW would resolve (key fields)")
    print("  is_new_quarter pivot: finviz date vs newest parquet report_date")
    print("  meta eps_surprise parsed:", ss.ScannerApp._parse_pct_value(meta.get("eps_surprise")))
    print("  meta sales_surprise parsed:", ss.ScannerApp._parse_pct_value(meta.get("sales_surprise")))
    print("\nDONE.")


if __name__ == "__main__":
    main()
