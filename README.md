# Trader's Automated News Scanner

A real-time news and SEC filing scanner for active traders on Windows. Morning Scanner watches your TradeStation Market Depth window, detects which ticker you're viewing, and automatically pulls up relevant news headlines, SEC filing activity, float data, short interest, and shelf registration status.

---

## Features

- **Automatic symbol detection** — Reads the active ticker from your TradeStation Market Depth / Matrix window title in real time. No manual input needed.
- **News aggregation** — Combines headlines from Finviz, GlobeNewswire, PRNewswire, and Yahoo Finance into a single chronological feed.
- **SEC filing awareness**
  - **SEC recency** — Color-coded indicator showing whether the company filed anything with the SEC in the last 24h (hot), 48h (warm), or longer (cold). Click to open EDGAR.
  - **Shelf registration** — Checks for active S-3 filings within the last 3 years. Click to view on EDGAR.
- **CIK resolution** — Maps tickers to SEC CIK numbers using the official SEC company tickers list, with fuzzy name matching as a fallback.
- **Float and short data** — Displays shares float (highlighted green if under 20M) and short float percentage from Finviz.
- **Catalyst detection** — Extracts intraday catalyst text from Finviz when available.
- **Keyword highlighting** — Enter comma-separated keywords to highlight matching headlines in the news table.
- **Time filtering** — Toggle between Today only, 48h, or All news.
- **Status indicators** — Live colored dots showing feed health for each data source (PR, GB, YH, FV, SEC).
- **Clickable links** — Double-click any headline to open it in your browser. Click the company name to open its Finviz page.
- **Dark / Light theme** — Toggle between themes; persists across sessions.
- **Adjustable font size** — `+` / `-` buttons to scale the entire UI.
- **Always on top** — Floats above your charting platform.
- **Persistent settings** — Window geometry, font size, and theme are saved to `scanner_settings.json`.

## Requirements

- **Windows only** — Uses `pywin32` (`win32gui`) for TradeStation window enumeration.
- Python 3.10+
- Python packages:
  - `requests`
  - `beautifulsoup4`
  - `pywin32`

Install dependencies:

```
pip install -r requirements.txt
```

Or individually:

```
pip install requests beautifulsoup4 pywin32
```

## Usage

1. Open TradeStation and have a Market Depth or Matrix window visible.
2. Run the scanner:
   ```
   python scan_sec.py
   ```
3. The scanner detects the ticker from your Market Depth window title and automatically loads news, SEC data, and market metadata.
4. Switch tickers in TradeStation — the scanner follows automatically.

### Controls

| Control | Action |
|---------|--------|
| **48h** checkbox | Show headlines from the last 48 hours |
| **All** checkbox | Show all cached headlines |
| **MCap** checkbox | Display market cap in the metadata bar |
| **Rel Vol** checkbox | Display relative volume in the metadata bar |
| **Highlight** field | Comma-separated keywords to highlight in the news table |
| **+** / **-** buttons | Increase / decrease font size |
| **Sun/Moon** button | Toggle dark / light theme |
| Double-click headline | Open article in browser |
| Click company name | Open Finviz quote page |
| Click **SEC:** label | Open EDGAR recent filings |
| Click **Shelf:** label | Open EDGAR S-3 filings |

## Data Sources

| Source | What it provides | Update frequency |
|--------|-----------------|------------------|
| [Finviz](https://finviz.com) | Company name, float, short interest, market cap, relative volume, news, catalyst | On each symbol change |
| [SEC EDGAR](https://www.sec.gov) | CIK mapping, recent filing recency, S-3 shelf registration | On each symbol change |
| [GlobeNewswire](https://www.globenewswire.com) | Press release headlines | Every 60 seconds (RSS) |
| [PRNewswire](https://www.prnewswire.com) | Press release headlines | Every 60 seconds (RSS) |
| [Yahoo Finance](https://finance.yahoo.com) | General market news headlines | Every 60 seconds (RSS) |

## Project Structure

```
MorningScanner/
  scan_sec.py             # Application source (single file)
  requirements.txt        # Python dependencies
  scanner_settings.json   # Auto-generated settings (geometry, font, theme)
  wires_cache.json        # Auto-generated RSS headline cache (pruned to 7 days)
  sec_tickers.json        # Auto-generated SEC CIK ticker map cache
  icon2.ico               # Application icon
  icon3.ico               # Alternate icon
  Scanner_SEC.spec        # PyInstaller build spec
  dist/
    Scanner_SEC.exe       # Standalone executable
```

## Building the .exe

From the `MorningScanner/` directory with PyInstaller installed:

```
pip install pyinstaller
pyinstaller --onefile --noconsole --name Scanner_SEC --icon icon2.ico ^
  --hidden-import win32gui --hidden-import win32api ^
  scan_sec.py
```

## Limitations

- **Windows only** — Requires `pywin32` for window title enumeration.
- **TradeStation only** — Symbol detection looks for windows with "TradeStation" in the title containing Market Depth / Matrix panels. Other platforms are not supported.
- **Web scraping fragility** — Finviz and SEC EDGAR data is extracted by scraping HTML. Site redesigns can silently break data extraction. The status indicators will show ERR if a source fails.
- **SEC EDGAR legacy endpoint** — Uses the `cgi-bin/browse-edgar` endpoint which SEC may deprecate in favor of EFTS.
- **Rate limiting** — Finviz requests are throttled to one every 2.2 seconds to avoid being blocked. Rapid symbol switching may delay data.
- **No real-time price data** — This is a news and filing scanner, not a price feed.

---

## Disclaimer

**This software is provided for educational and informational purposes only. It is not financial advice, and nothing in this application constitutes a recommendation to buy, sell, or hold any security.**

- All data displayed (news headlines, SEC filing dates, float figures, short interest) is sourced from public websites and may be delayed, incomplete, or inaccurate.
- The scanner **does not account for slippage, commissions, fees, or market impact**. It provides no trading signals or execution capabilities.
- SEC filing detection (shelf registration, recent filings) is based on HTML scraping and **is not guaranteed to be accurate or complete**. Always verify filings directly on [EDGAR](https://www.sec.gov/edgar/searchedgar/companysearch).
- The authors and contributors of this software accept **no liability** for any trading losses, errors, or damages arising from the use of this tool.
- **Use at your own risk.** You are solely responsible for your own trading decisions.
