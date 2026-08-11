# Morning Scanner — release 2026-08-11

**Binary:** `TNS.exe` · 89,760,932 bytes · built 2026-08-11 from `main` @ `da31320`
**Type:** stability + data-integrity release. No new features, no settings changes, no UI relayout.

Drop the new `TNS.exe` in place of the old one. Your `scanner_settings.json`,
`wires_cache.json`, `sec_tickers.json` and both ETF maps are untouched and carry
over — nothing needs re-configuring.

---

## Why this release exists

A full audit of the codebase (robustness / efficiency / security) produced 99
independently-verified findings, 93 of them demonstrated by running code rather
than by reading it. This release fixes **every high and medium one**: 3 high, 10
medium.

They fall into two groups, and both matter for the same reason — the app was
capable of looking completely healthy while being wrong.

---

## Group 1 — It could show you a wrong number

These are the ones worth reading.

### The earnings chart could plot another company's financials

Opening the earnings pop-out starts a background load that can take several
seconds on a cold cache. It correctly captured *which symbol* you asked for — but
then read the CIK and the Finviz snapshot **live** while it worked. If your chart
moved to a different ticker during that window, the pop-out came back titled with
the ticker you asked for and filled with the other company's EDGAR filings, EPS,
revenue and surprise figures. Nothing marked them as foreign; they were counted
and coloured exactly like real rows.

Now the symbol, CIK and metadata are captured together the moment you open the
chart, and a load whose symbol has gone stale is discarded rather than drawn.

### The earnings row could splice two different quarters together

Deciding whether Finviz was showing a quarter your local parquet hadn't ingested
yet came down to one assumption: *consecutive quarters are more than 50 days
apart*. Measured against your own 147,624-row earnings file, that assumption is
wrong for **3.88% of real quarter transitions — 700 of them in the last 12
months**, across names like ICLR (36 days), QMCO (46) and POWW (49).

When it was wrong you got a single row that read as one coherent quarter but
wasn't: today's date and today's beat/miss sitting beside **last quarter's**
YoY and period. Nothing flagged it, and the async correction that would normally
repair the YoY was explicitly switched off for that path.

The question is now the answerable one — *do the parquet row and the live date
describe the same reporting event?* — instead of a guess about quarter cadence.

### Impossible rows could win

Your parquet contains 29 rows whose fiscal period *ends after the date they were
reported* — physically impossible, and not caught by any existing filter. One of
those could win the "most recent report" pick and then feed the year-ago
comparison, quietly selecting the wrong quarter's baseline. Duplicate rows for
the same report were resolved by whichever happened to sit first in the file.

Corrupt rows are now dropped, and genuine duplicates are resolved by how
plausible the period→report gap is, then by source preference. Concretely: IMMR
on 2024-08-20 now shows **+78.00%** instead of **+640.26%**.

### "Shelf: NO" could mean "we never got an answer"

If SEC returned a transient error — a rate-limit or a 5xx — the app remembered
that failure for the rest of the session, stopped asking, and displayed
**"Shelf: NO"** and **"SEC: >48h"** as though they were findings. The SEC status
light stayed **green** throughout. The same wrong assertion appeared for any
symbol whose CIK hadn't resolved, which happens routinely for ETFs, index
symbols, and every symbol change during the first seconds after launch.

Unknown is now shown as unknown (`Shelf: —`, `SEC: —`), and transient errors are
retried on the next symbol change instead of being cached.

---

## Group 2 — It could stop working without telling you

### One bad data file could silently freeze the news list

If the earnings parquet drifted — a renamed column, a timezone-aware timestamp, a
column that arrived as text — the failure landed in the middle of the repaint,
*before* the two lines that refresh the news list and stamp "Last Refreshed". The
result on every symbol change: the wire list never updated, the clock froze, and
the previous ticker's earnings stayed on screen under the new symbol's name. In a
windowed build there is no console, so nothing was printed anywhere.

Worth knowing: this needed no upstream change to trigger. Pointing the parquet
setting at a neighbouring file in the same folder — a one-character-different
filename — was enough, and the setting persists across restarts.

The file is now validated and type-normalized once when loaded, and the repaint
degrades to a blank earnings row instead of taking the news list down with it.

### The refresh button could die permanently

There was exactly one line that re-enabled `↻`, and two ordinary situations
skipped it: changing chart symbols while a refresh was in flight, and any error
during the final repaint. Since a disabled button can't be clicked, the only
recovery was restarting the app. Both paths now always release it.

### The app could hang forever on exit or on save

If the folder TNS.exe lives in is write-protected by permissions rather than by
the read-only flag — Program Files, a managed work machine, or Windows Defender's
Controlled Folder Access, which specifically targets *unsigned* apps like this one
— the settings writer entered a loop that would have taken roughly **54 hours at
100% CPU** to exit, and never reported an error. It began on launch, silently,
long before you'd notice. It now fails immediately and visibly instead.

### The window could open where you can't see it

Position is saved on exit. Run on a second monitor to the left of your primary,
undock, and relaunch: the window was restored to coordinates that no longer exist
and opened entirely off-screen, which reads as "the app didn't start". Saved
positions are now checked against the monitors you actually have.

### A compromised Finviz could freeze the app permanently

Scraped values were stored without any length limit and then rendered directly
into the header on the UI thread, and the earnings-date parser ran patterns over
them whose cost grew with the *square* of the input. A hostile or hijacked page
could freeze the app for hours — measured at 7.9 seconds for a 20,000-character
value, and effectively forever at the response size limit — recurring on every
repaint until the process was killed. Values are now capped where they're read,
and the parsers are linear.

---

## Also in this release

- `requirements.txt` now describes the environment the exe is actually built from.
  The previous file was wrong on 11 of 13 pins and named the wrong Python version,
  so rebuilding from it produced a materially different binary — across exactly the
  libraries the earnings features depend on.
- The Historical Lookup button got the same always-release treatment as `↻`.
- Non-finite values (`nan`, `inf`) can no longer reach a percentage or market-cap
  cell as a formatted number.

## Verification

- Smoketest expanded from 28 to **37 tests** — one per fix, each asserting the
  specific failure can't recur — **all passing**.
- Every earnings change re-checked against the live 147,624-row parquet: the file
  loads unchanged, exactly the 29 corrupt rows are isolated, and the audit's known
  bad cases now resolve correctly.
- Rebuilt exe launched and confirmed running.

## Known limitations (unchanged)

- The binary is **unsigned**, so SmartScreen will warn on first run and Defender's
  Controlled Folder Access will refuse it write access if enabled.
- Scraping remains inherently fragile to site redesigns; the status dots show
  `ERR` when a source fails.
- Findings rated *low* and *info* in the audit are not addressed here. The audit
  report lists them with severities and suggested fixes.
