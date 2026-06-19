"""Scrape public issuer product-list pages for single-stock leveraged/inverse ETFs.

Invoked from the Settings dialog's "Refresh now" button. Runs in a
daemon thread so the Tk main loop never blocks. Per-issuer functions
are independent — one failure does not abort the run.

Design (revised after the first build's text-window pairing produced
garbage rows where each ETF's "ticker" was actually the next product's
underlying):

* **Row-based pairing.** Each scraper walks the page's HTML table rows
  (or equivalent grid blocks) and extracts ticker + product name from
  the *same* row. No more 200-char text-window matching.
* **Sanity filter** rejects results where ticker == underlying, the
  ticker is a known stopword, or it doesn't look like a 2–5 char
  alpha-uppercase symbol.
* **Preserve-on-failure.** If an issuer's scraper fails (network error
  or zero valid results), the orchestrator preserves prior entries for
  that issuer from the existing map. A 404 or layout change doesn't
  wipe known-good data.
* Short request timeouts (15s) so a hung CDN can't freeze a Refresh.
* No new dependencies — uses ``requests`` + ``BeautifulSoup``.

The aggregated return shape matches ``etf_map.EtfMap.replace()``:

    forward = {
        "TSLA": [
            {"ticker": "TSLL", "issuer": "Direxion", "mult": 2.0},
            ...
        ],
        ...
    }
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


REQUEST_TIMEOUT = 15  # seconds
# Hard cap on response body size for any single scraped page. Issuer
# product pages and per-product detail pages should both be well under
# 5 MB in normal use; this caps the worst-case allocation a hostile /
# misbehaving origin could trigger.
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _read_capped_bytes(response, max_bytes):
    """Drain a streamed Response in 64 KB chunks, raising ``ValueError``
    if the body exceeds ``max_bytes``."""
    buf = bytearray()
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise ValueError(f"response exceeded {max_bytes} bytes")
    return bytes(buf)


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _retry_after_seconds(resp, default: float) -> float:
    """Honor a server ``Retry-After`` (seconds form) up to a 10s ceiling,
    else fall back to ``default``."""
    try:
        ra = resp.headers.get("Retry-After")
        if ra:
            return min(10.0, max(0.0, float(ra)))
    except (ValueError, TypeError, AttributeError):
        pass
    return default


def _get_streamed_with_retry(session: requests.Session, url: str,
                             attempts: int = 3, base_delay: float = 0.5):
    """Streamed GET with a small retry-with-backoff for TRANSIENT failures
    (429 / 5xx / network errors), honoring Retry-After. Returns the final
    Response (caller validates status + reads the body) or None if every
    attempt failed at the network layer. A permanent status (e.g. 404) is
    returned immediately without retrying. Runs on the ETF refresh daemon
    thread, so the backoff sleeps never block the UI."""
    last = None
    for i in range(attempts):
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
        except requests.RequestException:
            r = None
        if r is not None and r.status_code not in _RETRYABLE_STATUS:
            return r  # success or a permanent (non-retryable) status
        # Transient: back off and retry (unless this was the last attempt).
        if i < attempts - 1:
            delay = base_delay * (2 ** i)
            if r is not None:
                delay = _retry_after_seconds(r, delay)
                try: r.close()
                except Exception: pass
            time.sleep(min(delay, 10.0))
        last = r
    return last


def _host_on_apex(url: str, apex: str) -> bool:
    """True only if ``url`` is https and its host is ``apex`` or a
    subdomain of it. Used to pin sitemap-derived / redirected fetches to
    the issuer's own domain — a poisoned sitemap <loc> or an off-host
    redirect (an SSRF pivot to internal/exfil hosts) is refused even
    though the path-shape regex might still match it."""
    try:
        pu = urlparse(url)
    except (ValueError, AttributeError):
        return False
    host = (pu.hostname or "").lower()
    apex = (apex or "").lower()
    return pu.scheme == "https" and bool(apex) and (
        host == apex or host.endswith("." + apex))

ProgressCb = Callable[[str], None]


# Common false-positive ticker tokens we never want to pair as an ETF symbol.
_STOPWORD_TICKERS = {
    "ETF", "ETFS", "USD", "NYSE", "BATS", "NASDAQ", "ARCA", "OTC",
    "FUND", "FUNDS", "DAILY", "BULL", "BEAR", "LONG", "SHORT",
    "INVERSE", "LEVERAGED", "TARGET", "INDEX",
    "REX", "TREX", "AXS", "GRANITESHARES", "DIREXION", "DEFIANCE", "TRADR",
    "ICR",   # ICR often appears on issuer index banners
    "USA", "USX",
}

# Underlyings we explicitly reject — crypto and rates products that
# share the ETF naming convention but aren't single STOCKS. The user
# asked for single-stock products only.
_NON_STOCK_UNDERLYINGS = {
    "BTC", "ETH", "ETHE", "BCH", "LTC", "SOL", "XRP", "ADA",
    "DOGE", "AVAX", "DOT", "LINK", "USDC", "USDT",
    "TBIL", "TBLT", "TLT", "GLD", "SLV", "USO",
}

# Title-substring filters: if any of these words appears in the product
# name we skip the entry (treasury / income / commodity / crypto plays).
_NON_STOCK_TITLE_WORDS = (
    "treasury", "bill", "income", "bond",
    "bitcoin", "ether ", "crypto", "yieldboost",
    "gold", "silver", "oil",
)


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log(progress_cb: ProgressCb, msg: str) -> None:
    try:
        progress_cb(f"[{_ts()}] {msg}")
    except Exception:
        pass  # never let a UI callback crash a scrape


def _is_sane_entry(underlying: str, ticker: str) -> bool:
    """Filter out obvious junk pairings before they reach the map."""
    if not underlying or not ticker:
        return False
    if underlying == ticker:
        return False
    if not (2 <= len(ticker) <= 5):
        return False
    if not (1 <= len(underlying) <= 5):
        return False
    if not ticker.isalpha() or not ticker.isupper():
        return False
    if not underlying.isalpha() or not underlying.isupper():
        return False
    if ticker in _STOPWORD_TICKERS or underlying in _STOPWORD_TICKERS:
        return False
    return True


def _direction_sign(word: str) -> int:
    w = (word or "").strip().lower()
    if w in {"long", "bull", "bullish", "up"} or w.startswith("long") or w.startswith("bull"):
        return 1
    if w in {"short", "bear", "bearish", "inverse", "down"} or w.startswith("short") or w.startswith("bear") or w.startswith("inverse"):
        return -1
    return 0


def _fetch_html(session: requests.Session, url: str) -> str:
    # These issuer scrapers fetch a small set of author-controlled https
    # literals that legitimately span hosts (e.g. direxion.com + a
    # fallback to etfdb.com), so we don't host-pin — but require https on
    # both the request and the final landing URL so a compromised origin
    # can't redirect us to a downgraded/non-https endpoint.
    if urlparse(url).scheme != "https":
        raise RuntimeError(f"refusing non-https URL {url}")
    r = _get_streamed_with_retry(session, url)
    if r is None:
        raise RuntimeError(f"network error for {url}")
    try:
        if urlparse(r.url).scheme != "https":
            raise RuntimeError(f"refusing non-https redirect to {r.url}")
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} for {url}")
        raw = _read_capped_bytes(r, _MAX_RESPONSE_BYTES)
    finally:
        try: r.close()
        except Exception: pass
    # Honor charset hint if the server provided one; else default UTF-8.
    encoding = (r.encoding or "utf-8")
    try:
        return raw.decode(encoding, errors="replace")
    except (LookupError, TypeError):
        return raw.decode("utf-8", errors="replace")


def _walk_rows(html: str) -> list[tuple[list[str], str]]:
    """Yield (cell_texts, joined_text) for every <tr> in the document.

    Falls back to any grouped <div> / <li> / list-row-shaped container
    when the page doesn't use <table>. The joined string is whitespace-
    normalized so regex patterns can match across cell boundaries.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[list[str], str]] = []
    # First pass: real table rows.
    for tr in soup.find_all("tr"):
        cells = [
            td.get_text(separator=" ", strip=True)
            for td in tr.find_all(["td", "th"])
        ]
        if cells:
            out.append((cells, " | ".join(cells)))
    if out:
        return out
    # Fallback: rows-shaped containers — common on JS-rendered tables
    # that ship server-side as nested divs.
    for div in soup.find_all(
        ["div", "li", "article"],
        class_=lambda v: v and any(
            k in (v if isinstance(v, str) else " ".join(v)).lower()
            for k in ("row", "card", "fund", "etf", "product")
        ),
    ):
        text = div.get_text(separator=" ", strip=True)
        if text:
            cells = [s.strip() for s in re.split(r"\s{2,}|\|", text) if s.strip()]
            out.append((cells, text))
    return out


def _extract_ticker_from_row(
    cells: list[str], *, exclude: set[str],
) -> Optional[str]:
    """Pick the most plausible ETF ticker from a row's cells.

    Returns the first 2-5 char alpha-upper cell that isn't a stopword
    and isn't the underlying. ETF tickers on issuer pages are usually
    in their own dedicated cell, so a strict full-cell match wins
    over loose substring matches.
    """
    # Pass 1: cell is exactly a ticker.
    for c in cells:
        c = c.strip()
        if (
            re.fullmatch(r"[A-Z]{2,5}", c)
            and c not in exclude
            and c not in _STOPWORD_TICKERS
        ):
            return c
    # Pass 2: ticker as the first token of a cell (e.g. "TSLL Direxion Daily TSLA Bull 2X Shares").
    for c in cells:
        m = re.match(r"^([A-Z]{2,5})\b", c.strip())
        if m:
            sym = m.group(1)
            if sym not in exclude and sym not in _STOPWORD_TICKERS:
                return sym
    return None


def scrape_all(
    progress_cb: ProgressCb,
    *,
    existing_forward: Optional[dict[str, list[dict]]] = None,
    issuers: Optional[Iterable[str]] = None,
) -> tuple[dict[str, list[dict]], list[str], list[str]]:
    """Run every issuer scraper and merge the results.

    ``existing_forward`` lets us preserve prior entries for any issuer
    whose live scrape fails — the seed JSON or the user's last good
    refresh isn't wiped out by a transient 404.

    Returns ``(forward_map, issuers_scraped, errors)``.
    """
    all_scrapers = [
        ("Direxion",      scrape_direxion),
        ("GraniteShares", scrape_graniteshares),
        ("T-Rex",         scrape_trex),
        ("Tradr",         scrape_tradr),   # AXS rebrand
        ("Defiance",      scrape_defiance),
    ]
    if issuers is not None:
        wanted = {x.lower() for x in issuers}
        all_scrapers = [(n, f) for n, f in all_scrapers if n.lower() in wanted]

    session = requests.Session()
    session.headers.update({
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    # Per-issuer success state — drives the preserve-on-failure logic.
    per_issuer_rows: dict[str, list[dict]] = {n: [] for n, _ in all_scrapers}
    succeeded: set[str] = set()
    scraped: list[str] = []
    errors: list[str] = []

    _log(progress_cb, f"Starting refresh ({len(all_scrapers)} issuers)...")
    for name, fn in all_scrapers:
        _log(progress_cb, f"[{name}] fetching product list...")
        t0 = time.monotonic()
        try:
            rows = fn(session, progress_cb)
        except requests.RequestException as exc:
            errors.append(f"{name}: network error: {exc}")
            _log(progress_cb, f"[{name}] NETWORK ERROR: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc!r}")
            _log(progress_cb, f"[{name}] ERROR: {exc!r}")
            continue
        dt = time.monotonic() - t0
        # Sanity-filter + attach issuer label.
        clean: list[dict] = []
        for row in rows:
            und = (row.get("underlying") or "").upper().strip()
            tk = (row.get("ticker") or "").upper().strip()
            mult = row.get("mult")
            if mult is None or not _is_sane_entry(und, tk):
                continue
            try:
                m = float(mult)
            except (TypeError, ValueError):
                continue
            clean.append({"underlying": und, "ticker": tk, "issuer": name, "mult": m})
        n_etfs = len(clean)
        unique_und = {r["underlying"] for r in clean}
        if n_etfs == 0:
            errors.append(f"{name}: 0 valid entries after sanity filter")
            _log(
                progress_cb,
                f"[{name}] 0 ETFs after sanity filter ({dt:.1f}s) — preserving prior data",
            )
            continue
        # Partial-result guard: if we already had a healthy count for
        # this issuer and the scrape returned far less, the page is
        # likely JS-rendered and we got only the featured subset.
        # Don't wipe known-good entries — preserve like a full failure.
        prior_count = 0
        if existing_forward:
            prior_count = sum(
                1
                for etfs in existing_forward.values()
                for e in etfs
                if isinstance(e, dict) and e.get("issuer") == name
            )
        if prior_count > 5 and n_etfs < max(5, int(prior_count * 0.3)):
            errors.append(
                f"{name}: partial result ({n_etfs} scraped vs {prior_count} prior) "
                f"— preserving prior data"
            )
            _log(
                progress_cb,
                f"[{name}] PARTIAL ({n_etfs} < 30% of {prior_count} prior) "
                f"({dt:.1f}s) — preserving prior data",
            )
            continue
        per_issuer_rows[name] = clean
        succeeded.add(name)
        scraped.append(name)
        _log(
            progress_cb,
            f"[{name}] {n_etfs} ETFs across {len(unique_und)} underlyings ({dt:.1f}s)",
        )

    # Merge: succeeded issuers replace; failed issuers preserve from
    # ``existing_forward``. This is the bit that protects the seed
    # against a Direxion 404 wiping every TSLL/NVDU entry.
    forward: dict[str, list[dict]] = {}
    if existing_forward:
        for und, etfs in existing_forward.items():
            kept = [
                e for e in etfs
                if isinstance(e, dict)
                and e.get("issuer") not in succeeded
            ]
            if kept:
                forward[und] = list(kept)
    # Now layer in the freshly-scraped rows.
    for name, rows in per_issuer_rows.items():
        for r in rows:
            und = r["underlying"]
            forward.setdefault(und, []).append({
                "ticker": r["ticker"],
                "issuer": name,
                "mult": r["mult"],
            })

    total_etfs = sum(len(v) for v in forward.values())
    _log(
        progress_cb,
        f"Done. {len(forward)} underlyings, {total_etfs} ETFs total. "
        f"{len(errors)} error(s).",
    )
    return forward, scraped, errors


# --- per-issuer scrapers ----------------------------------------------------
# Each returns a list of {"underlying", "ticker", "mult"} dicts. The
# orchestrator adds the issuer label and runs the sanity filter.


def scrape_direxion(session: requests.Session, progress_cb: ProgressCb) -> list[dict]:
    """Direxion single-stock leveraged ETFs.

    Direxion's main page returns 403 to plain requests, so we try a few
    known mirrors and SEC filings. If none reach, we raise — the
    orchestrator preserves any Direxion entries from the existing map.
    """
    urls = [
        "https://www.direxion.com/products?filter=single-stock",
        "https://www.direxion.com/our-etfs",
        "https://etfdb.com/etfs/issuers/direxion/",
    ]
    last_err: Optional[Exception] = None
    html: Optional[str] = None
    for url in urls:
        try:
            html = _fetch_html(session, url)
            _log(progress_cb, f"[Direxion]   reached {url}")
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            _log(progress_cb, f"[Direxion]   miss {url}: {exc}")
    if html is None:
        raise RuntimeError(f"no Direxion source reachable: {last_err!r}")

    name_re = re.compile(
        r"Daily\s+([A-Z]{1,5})\s+(Bull|Bear)\s+([0-9]+(?:\.[0-9]+)?)X",
        re.IGNORECASE,
    )
    rows = _walk_rows(html)
    out: list[dict] = []
    for cells, joined in rows:
        # Per-row fault isolation (see scrape_graniteshares).
        try:
            m = name_re.search(joined)
            if not m:
                continue
            underlying = m.group(1).upper()
            direction = m.group(2)
            mult_raw = float(m.group(3))
            sign = _direction_sign(direction)
            if sign == 0:
                continue
            ticker = _extract_ticker_from_row(cells, exclude={underlying})
            if not ticker:
                continue
            out.append({
                "underlying": underlying,
                "ticker": ticker,
                "mult": mult_raw * sign,
            })
        except Exception as exc:  # noqa: BLE001 — isolate one bad row
            _log(progress_cb, f"[Direxion]   row error: {type(exc).__name__}")
            continue
    return _dedupe(out)


def _is_stock_underlying(underlying: str, title: str) -> bool:
    """Reject crypto / treasury / income / commodity ETFs that share
    the naming convention but aren't single STOCKS."""
    if underlying in _NON_STOCK_UNDERLYINGS:
        return False
    lo = (title or "").lower()
    return not any(w in lo for w in _NON_STOCK_TITLE_WORDS)


def _walk_sitemap(
    session: requests.Session, sitemap_url: str, url_pattern: re.Pattern,
    apex: str,
) -> list[str]:
    """Return every <loc> URL in the sitemap (recursing into index
    sitemaps once) whose path matches ``url_pattern`` AND whose host is
    pinned to ``apex`` (anti-SSRF: a poisoned sitemap can't redirect the
    follow-up fetches to an attacker-chosen host).

    Sitemap responses are streamed in 64 KB chunks with a 5 MB hard cap
    so a misbehaving origin can't balloon memory."""
    out: list[str] = []
    body = _safe_fetch_text(session, sitemap_url, allowed_host=apex)
    if body is None:
        raise RuntimeError(f"sitemap fetch failed for {sitemap_url}")
    locs = re.findall(r"<loc>([^<]+)</loc>", body)
    def _ok(u: str) -> bool:
        return bool(url_pattern.search(u)) and _host_on_apex(u, apex)
    # If the top-level is a sitemap index (no direct matches and entries
    # look like .xml children), follow each child once.
    direct = [u for u in locs if _ok(u)]
    if direct:
        return direct
    children = [u for u in locs
                if u.lower().endswith(".xml") and _host_on_apex(u, apex)]
    for child in children:
        sub_body = _safe_fetch_text(session, child, allowed_host=apex)
        if sub_body is None:
            continue
        sub_locs = re.findall(r"<loc>([^<]+)</loc>", sub_body)
        out.extend(u for u in sub_locs if _ok(u))
    return out


def _safe_fetch_text(session: requests.Session, url: str, allowed_host=None):
    """Streamed + capped GET; returns the decoded body text or None on
    any error (non-200, size cap, network). When ``allowed_host`` is
    given, the requested URL — and the final URL after any redirects —
    must be https on that apex, else the fetch is refused (anti-SSRF)."""
    if allowed_host is not None and not _host_on_apex(url, allowed_host):
        return None
    r = _get_streamed_with_retry(session, url)
    if r is None:
        return None
    try:
        # Refuse the body if a redirect carried us off the issuer's
        # domain (or downgraded scheme) — requests follows redirects by
        # default, so check where we actually landed.
        if allowed_host is not None and not _host_on_apex(r.url, allowed_host):
            return None
        if r.status_code != 200:
            return None
        try:
            raw = _read_capped_bytes(r, _MAX_RESPONSE_BYTES)
        except (ValueError, requests.RequestException, OSError):
            return None
    finally:
        try: r.close()
        except Exception: pass
    encoding = r.encoding or "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except (LookupError, TypeError):
        return raw.decode("utf-8", errors="replace")


def scrape_graniteshares(
    session: requests.Session, progress_cb: ProgressCb,
) -> list[dict]:
    """GraniteShares single-stock leveraged / inverse ETFs.

    The /etfs/ landing page is JS-rendered, so we drive off the
    sitemap instead. Each product has its own static page at
    ``/etfs/{ticker}/`` whose ``<title>`` carries the full marketing
    name (``GraniteShares 2x Long NVDA Daily ETF``) — clean to parse.
    """
    sitemap = "https://graniteshares.com/sitemap.xml"
    apex = "graniteshares.com"
    url_pat = re.compile(r"graniteshares\.com/etfs/[a-z0-9-]+/?$", re.IGNORECASE)
    urls = _walk_sitemap(session, sitemap, url_pat, apex)
    if not urls:
        raise RuntimeError("sitemap yielded no GraniteShares ETF URLs")
    _log(progress_cb, f"[GraniteShares]   sitemap: {len(urls)} ETF URLs")

    name_re = re.compile(
        r"GraniteShares\s+([0-9]+(?:\.[0-9]+)?)x\s+(Long|Short)\s+"
        r"([A-Z]{1,5})\s+Daily\s+ETF",
        re.IGNORECASE,
    )
    out: list[dict] = []
    polite_delay = 0.15  # seconds between product fetches
    for i, url in enumerate(urls):
        # Per-row fault isolation: one anomalous product page must not
        # abort the whole issuer (the orchestrator's catch-all would
        # otherwise discard every row scraped so far for GraniteShares).
        try:
            body = _safe_fetch_text(session, url, allowed_host=apex)
            if body is None:
                continue
            title_m = re.search(r"<title>([^<]+)</title>", body, re.IGNORECASE)
            # Cap before regex matching: a pathological multi-KB <title>
            # would make the lazy title_re do O(n^2) work on the daemon
            # thread. 400 chars comfortably covers any real product name.
            title = (title_m.group(1) if title_m else "")[:400]
            nm = name_re.search(title)
            if not nm:
                # Some products (YieldBOOST income, treasury) don't match.
                time.sleep(polite_delay)
                continue
            mult_raw = float(nm.group(1))
            direction = nm.group(2)
            underlying = nm.group(3).upper()
            sign = _direction_sign(direction)
            if sign == 0:
                time.sleep(polite_delay)
                continue
            # Ticker = the URL's trailing slug. Fallback to the title's
            # parenthetical (e.g. ``... (NVDL) |``) if the slug doesn't
            # look like a 2-5 char alpha-upper symbol.
            slug = url.rstrip("/").rsplit("/", 1)[-1].upper()
            ticker = slug if re.fullmatch(r"[A-Z]{2,5}", slug) else None
            if not ticker:
                pm = re.search(r"\(([A-Z]{2,5})\)", title)
                ticker = pm.group(1) if pm else None
            if not ticker or not _is_stock_underlying(underlying, title):
                time.sleep(polite_delay)
                continue
            out.append({
                "underlying": underlying,
                "ticker": ticker,
                "mult": mult_raw * sign,
            })
            # Throttled progress: log every 10th page so the user sees
            # life without spamming the terminal.
            if (i + 1) % 10 == 0:
                _log(progress_cb, f"[GraniteShares]   ...{i + 1}/{len(urls)} pages")
        except Exception as exc:  # noqa: BLE001 — isolate one bad row
            _log(progress_cb, f"[GraniteShares]   row error: {type(exc).__name__}")
            continue
        time.sleep(polite_delay)
    return _dedupe(out)


def scrape_trex(session: requests.Session, progress_cb: ProgressCb) -> list[dict]:
    """T-Rex 2X Long / 2X Short single-stock ETFs (REX Shares brand).

    REX's product listing page is fully JS-rendered, so we drive off
    the WordPress page-sitemap instead. Each ETF has a static page at
    ``rexshares.com/{ticker}/`` whose ``<title>`` follows
    ``{TICKER} - T-REX {N}X (Long|Short|Inverse) {Company} ETF | {N}X {SYM} Daily``.
    Both the leverage details AND the underlying stock ticker are in
    the title — clean, deterministic parse with no JS dependency.
    """
    sitemap = "https://www.rexshares.com/sitemap.xml"
    apex = "rexshares.com"
    # REX uses an index sitemap → page-sitemap.xml has the product
    # pages. Ticker-shaped path = 2-5 lowercase chars.
    url_pat = re.compile(r"rexshares\.com/[a-z]{2,5}/?$", re.IGNORECASE)
    urls = _walk_sitemap(session, sitemap, url_pat, apex)
    if not urls:
        raise RuntimeError("sitemap yielded no REX/T-Rex ETF URLs")
    _log(progress_cb, f"[T-Rex]   sitemap: {len(urls)} candidate URLs")

    # Title pattern: TICKER - T-REX 2X Long Company ETF | 2X SYM Daily
    title_re = re.compile(
        r"([A-Z]{2,5})\s*[-–]\s*T[-\s]?REX\s+([0-9]+(?:\.[0-9]+)?)X\s+"
        r"(Long|Short|Inverse)\s+[^|]*?\|\s*[-]?([0-9]+(?:\.[0-9]+)?)X\s+"
        r"([A-Z]{1,5})\s+Daily",
        re.IGNORECASE,
    )
    out: list[dict] = []
    polite_delay = 0.15
    for i, url in enumerate(urls):
        # Per-row fault isolation (see scrape_graniteshares).
        try:
            body = _safe_fetch_text(session, url, allowed_host=apex)
            if body is None:
                continue
            title_m = re.search(r"<title>([^<]+)</title>", body, re.IGNORECASE)
            # Cap before regex matching: a pathological multi-KB <title>
            # would make the lazy title_re do O(n^2) work on the daemon
            # thread. 400 chars comfortably covers any real product name.
            title = (title_m.group(1) if title_m else "")[:400]
            m = title_re.search(title)
            if not m:
                time.sleep(polite_delay)
                continue
            ticker = m.group(1).upper()
            mult_raw = float(m.group(2))
            direction = m.group(3)
            underlying = m.group(5).upper()
            sign = _direction_sign(direction)
            if sign == 0:
                time.sleep(polite_delay)
                continue
            if not _is_stock_underlying(underlying, title):
                time.sleep(polite_delay)
                continue
            out.append({
                "underlying": underlying,
                "ticker": ticker,
                "mult": mult_raw * sign,
            })
            if (i + 1) % 10 == 0:
                _log(progress_cb, f"[T-Rex]   ...{i + 1}/{len(urls)} pages")
            time.sleep(polite_delay)
        except Exception as exc:  # noqa: BLE001 — isolate one bad row
            _log(progress_cb, f"[T-Rex]   row error: {type(exc).__name__}")
            continue
    return _dedupe(out)


def scrape_tradr(session: requests.Session, progress_cb: ProgressCb) -> list[dict]:
    """Tradr (formerly AXS Investments) single-stock leveraged ETFs.

    URL: https://www.tradretfs.com/etfs — table-shaped product list.
    Names like ``Tradr 2X Long TSLA Daily ETF`` / ``Tradr 1.5X Short NVDA Daily ETF``.
    """
    urls = [
        "https://www.tradretfs.com/etfs",
        "https://tradretfs.com/etfs",
        "https://www.tradretfs.com/",
    ]
    html: Optional[str] = None
    last_err: Optional[Exception] = None
    for url in urls:
        try:
            html = _fetch_html(session, url)
            _log(progress_cb, f"[Tradr]   reached {url}")
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            _log(progress_cb, f"[Tradr]   miss {url}: {exc}")
    if html is None:
        raise RuntimeError(f"no Tradr source reachable: {last_err!r}")

    name_re = re.compile(
        r"(?:Tradr\s+)?([0-9]+(?:\.[0-9]+)?)X\s+(Long|Short|Bull|Bear)\s+([A-Z]{1,5})\s+Daily",
        re.IGNORECASE,
    )
    rows = _walk_rows(html)
    out: list[dict] = []
    for cells, joined in rows:
        # Per-row fault isolation (see scrape_graniteshares).
        try:
            m = name_re.search(joined)
            if not m:
                continue
            mult_raw = float(m.group(1))
            direction = m.group(2)
            underlying = m.group(3).upper()
            sign = _direction_sign(direction)
            if sign == 0:
                continue
            ticker = _extract_ticker_from_row(cells, exclude={underlying})
            if not ticker:
                continue
            out.append({
                "underlying": underlying,
                "ticker": ticker,
                "mult": mult_raw * sign,
            })
        except Exception as exc:  # noqa: BLE001 — isolate one bad row
            _log(progress_cb, f"[Tradr]   row error: {type(exc).__name__}")
            continue
    return _dedupe(out)


def scrape_defiance(session: requests.Session, progress_cb: ProgressCb) -> list[dict]:
    """Defiance Daily Target single-stock leveraged ETFs.

    Product page: https://www.defianceetfs.com/explore-our-etfs.
    Names follow ``Daily Target {N}X (Long|Short) {SYM} ETF``.
    """
    urls = [
        "https://www.defianceetfs.com/explore-our-etfs",
        "https://www.defianceetfs.com/etfs/",
        "https://www.defianceetfs.com/",
    ]
    html: Optional[str] = None
    last_err: Optional[Exception] = None
    for url in urls:
        try:
            html = _fetch_html(session, url)
            _log(progress_cb, f"[Defiance]   reached {url}")
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            _log(progress_cb, f"[Defiance]   miss {url}: {exc}")
    if html is None:
        raise RuntimeError(f"no Defiance source reachable: {last_err!r}")

    name_re = re.compile(
        r"Daily\s+Target\s+([0-9]+(?:\.[0-9]+)?)X\s+(Long|Short)\s+([A-Z]{1,5})\b",
        re.IGNORECASE,
    )
    rows = _walk_rows(html)
    out: list[dict] = []
    for cells, joined in rows:
        # Per-row fault isolation (see scrape_graniteshares).
        try:
            m = name_re.search(joined)
            if not m:
                continue
            mult_raw = float(m.group(1))
            direction = m.group(2)
            underlying = m.group(3).upper()
            sign = _direction_sign(direction)
            if sign == 0:
                continue
            ticker = _extract_ticker_from_row(cells, exclude={underlying})
            if not ticker:
                continue
            out.append({
                "underlying": underlying,
                "ticker": ticker,
                "mult": mult_raw * sign,
            })
        except Exception as exc:  # noqa: BLE001 — isolate one bad row
            _log(progress_cb, f"[Defiance]   row error: {type(exc).__name__}")
            continue
    return _dedupe(out)


def _dedupe(rows: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for r in rows:
        key = (r["underlying"], r["ticker"], r["mult"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# =============================================================================
# MULTI-HOLDING ETF HOLDINGS (stockanalysis.com)
# =============================================================================
# The single-stock scrapers above map a stock -> the leveraged ETFs that
# track ONLY it. This section covers the complementary universe: ETFs that
# hold a BASKET of securities (sector / broad-index / thematic funds, plus
# the leveraged index/sector funds). Source is stockanalysis.com's free,
# no-key JSON API, which returns each ETF's top-N holdings, sector mix,
# category, holding count, and a freshness date. See etf_holdings.EtfHoldings
# for storage + the derived reverse (stock -> ETFs) index.

STOCKANALYSIS_HOLDINGS_API = "https://api.stockanalysis.com/api/symbol/e/{sym}/holdings"
# stockanalysis returns top-25 holdings for free/anonymous access; cap to
# match so a future source change can't bloat the stored payload.
_HOLDINGS_TOP_N = 25
# Polite pacing between per-ETF requests on the refresh daemon thread.
_HOLDINGS_PACE_SEC = 0.35

# Curated multi-holding ETF universe with known leverage multiples (bear =
# negative, non-levered = None). Hardcoding leverage here is deliberate:
# the holdings API doesn't expose a reliable leverage field, and these are
# well-known funds, so an annotated list is more accurate than name-parsing
# and keeps the reverse map bounded + refreshable. Single-stock leveraged
# ETFs (TSLL etc.) are intentionally EXCLUDED — they live in etf_map.
CURATED_ETF_UNIVERSE: dict[str, "float | None"] = {
    # --- Broad US index (non-levered) ---
    "SPY": None, "VOO": None, "IVV": None, "VTI": None, "QQQ": None,
    "QQQM": None, "DIA": None, "IWM": None, "IWB": None, "IWV": None,
    "VTV": None, "VUG": None, "IWF": None, "IWD": None, "MDY": None,
    "IJH": None, "IJR": None, "RSP": None, "SCHX": None, "SCHB": None,
    "SCHG": None, "SPLG": None, "ITOT": None, "VV": None, "VXF": None,
    # --- Broad international (non-levered) ---
    "VEA": None, "VWO": None, "EFA": None, "EEM": None, "IEFA": None,
    "IEMG": None, "VT": None, "VXUS": None, "ACWI": None, "VGK": None,
    "EWJ": None, "INDA": None, "EWZ": None, "EWY": None, "EWT": None,
    # --- US sectors (non-levered) ---
    "XLK": None, "XLF": None, "XLE": None, "XLV": None, "XLI": None,
    "XLY": None, "XLP": None, "XLU": None, "XLB": None, "XLRE": None,
    "XLC": None, "VGT": None, "VHT": None, "VFH": None, "VDE": None,
    "VIS": None, "VPU": None, "VAW": None, "VNQ": None, "VOX": None,
    "VCR": None, "VDC": None,
    # --- Industry / thematic (non-levered) ---
    "SMH": None, "SOXX": None, "IGV": None, "XBI": None, "IBB": None,
    "KRE": None, "KBE": None, "ITB": None, "XHB": None, "JETS": None,
    "TAN": None, "ICLN": None, "LIT": None, "HACK": None, "BUG": None,
    "BOTZ": None, "ROBO": None, "ARKK": None, "ARKG": None, "ARKW": None,
    "ARKQ": None, "ARKF": None, "FINX": None, "SKYY": None, "CIBR": None,
    "XME": None, "XOP": None, "OIH": None, "GDX": None, "GDXJ": None,
    "KWEB": None, "FXI": None, "MCHI": None, "ITA": None, "PPA": None,
    "MOO": None, "XRT": None, "IYR": None, "IYT": None, "PAVE": None,
    "URA": None, "COPX": None, "MAGS": None, "QTUM": None, "WCLD": None,
    # --- Dividend / factor (non-levered) ---
    "SCHD": None, "VYM": None, "VIG": None, "DGRO": None, "NOBL": None,
    "HDV": None, "DVY": None, "SPYD": None, "MTUM": None, "QUAL": None,
    "USMV": None, "VLUE": None, "SPLV": None, "DGRW": None,
    # --- Leveraged broad index ---
    "TQQQ": 3.0, "SQQQ": -3.0, "QLD": 2.0, "QID": -2.0,
    "UPRO": 3.0, "SPXU": -3.0, "SPXL": 3.0, "SPXS": -3.0,
    "SSO": 2.0, "SDS": -2.0, "UDOW": 3.0, "SDOW": -3.0,
    "DDM": 2.0, "DXD": -2.0, "TNA": 3.0, "TZA": -3.0,
    "UWM": 2.0, "TWM": -2.0,
    # --- Leveraged sector / thematic ---
    "SOXL": 3.0, "SOXS": -3.0, "TECL": 3.0, "TECS": -3.0,
    "FAS": 3.0, "FAZ": -3.0, "LABU": 3.0, "LABD": -3.0,
    "ROM": 2.0, "USD": 2.0, "FNGU": 3.0, "FNGD": -3.0,
    "NAIL": 3.0, "DPST": 3.0, "DRN": 3.0, "DRV": -3.0,
    "CURE": 3.0, "RETL": 3.0, "WANT": 3.0, "DFEN": 3.0,
    "PILL": 3.0, "UTSL": 3.0, "WEBL": 3.0,
    "ERX": 2.0, "ERY": -2.0, "GUSH": 2.0, "DRIP": -2.0,
    "YINN": 3.0, "YANG": -3.0, "CWEB": 2.0,
    "NUGT": 2.0, "DUST": -2.0, "JNUG": 2.0, "JDST": -2.0,
}


def fetch_stockanalysis_holdings(
    session: requests.Session, symbol: str, top_n: int = _HOLDINGS_TOP_N,
) -> "dict | None":
    """Fetch one ETF's holdings profile from stockanalysis.com.

    Returns ``{"category", "sector_label", "count", "date", "holdings":
    [{ticker, name, weight}, ...]}`` (``mult`` is set by the caller from the
    curated universe), or ``None`` if the symbol isn't a recognized ETF
    (404) or the response is unusable. Raises only via the session for a
    genuine network exhaustion the caller wants to log.
    """
    # Lazy import to avoid a hard import cycle and keep this module usable
    # standalone; etf_holdings imports nothing from here.
    from etf_holdings import derive_sector_label

    url = STOCKANALYSIS_HOLDINGS_API.format(sym=symbol.upper().strip())
    r = _get_streamed_with_retry(session, url)
    if r is None:
        return None
    try:
        if r.status_code != 200:
            return None
        body = _read_capped_bytes(r, _MAX_RESPONSE_BYTES)
    finally:
        try:
            r.close()
        except Exception:
            pass
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, json.JSONDecodeError):
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    raw_holdings = data.get("holdings")
    if not isinstance(raw_holdings, list):
        return None
    holdings: list[dict] = []
    for h in raw_holdings:
        if not isinstance(h, dict):
            continue
        tk = str(h.get("s") or "").lstrip("$").upper().strip()
        if not tk:
            continue
        try:
            w = float(str(h.get("as") or "0").replace("%", "").strip())
        except (TypeError, ValueError):
            w = 0.0
        holdings.append({"ticker": tk, "name": str(h.get("n") or "").strip(),
                         "weight": w})
        if len(holdings) >= top_n:
            break
    info = data.get("infoTable") if isinstance(data.get("infoTable"), dict) else {}
    category = str(info.get("category") or "").strip()
    try:
        count = int(info.get("count") or data.get("count") or len(holdings))
    except (TypeError, ValueError):
        count = len(holdings)
    return {
        "category": category,
        "sector_label": derive_sector_label(category, data.get("sectors")),
        "count": count,
        "date": str(data.get("date") or "").strip(),
        "holdings": holdings,
    }


def scrape_etf_holdings(
    progress_cb: ProgressCb,
    *,
    existing_profiles: "dict | None" = None,
    universe: "dict | None" = None,
    pace: float = _HOLDINGS_PACE_SEC,
    max_etfs: "int | None" = None,
) -> "tuple[dict, list[str]]":
    """Fetch holdings for the curated multi-holding ETF universe.

    Per-ETF failures are isolated and the prior profile (from
    ``existing_profiles``) is preserved, so a transient 404 / rate-limit on
    one fund never wipes the rest. Returns ``(profiles, errors)`` shaped for
    ``EtfHoldings.replace``.
    """
    universe = universe if universe is not None else CURATED_ETF_UNIVERSE
    existing_profiles = existing_profiles or {}
    tickers = list(universe.keys())
    if max_etfs is not None:
        tickers = tickers[:max_etfs]

    session = requests.Session()
    session.headers.update({
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })

    profiles: dict[str, dict] = {}
    errors: list[str] = []
    n = len(tickers)
    _log(progress_cb, f"Fetching holdings for {n} ETFs (stockanalysis.com)...")
    ok = 0
    for i, etf in enumerate(tickers, 1):
        etf = etf.upper().strip()
        mult = universe.get(etf)
        try:
            prof = fetch_stockanalysis_holdings(session, etf)
        except requests.RequestException as exc:
            prof = None
            errors.append(f"{etf}: network error: {exc}")
        except Exception as exc:  # noqa: BLE001
            prof = None
            errors.append(f"{etf}: {exc!r}")
        if prof and len(prof.get("holdings", [])) >= 2:
            prof["mult"] = mult
            profiles[etf] = prof
            ok += 1
            if i % 20 == 0 or i == n:
                _log(progress_cb, f"  [{i}/{n}] {etf}: {len(prof['holdings'])} "
                                  f"holdings{' ('+str(mult)+'x)' if mult else ''}")
        else:
            # Preserve a prior good profile for this ETF on failure.
            prior = existing_profiles.get(etf)
            if isinstance(prior, dict) and prior.get("holdings"):
                prior = dict(prior)
                prior["mult"] = mult
                profiles[etf] = prior
                _log(progress_cb, f"  [{i}/{n}] {etf}: fetch failed — preserving prior")
            else:
                _log(progress_cb, f"  [{i}/{n}] {etf}: no data")
        if pace and i < n:
            time.sleep(pace)

    _log(progress_cb, f"Done. {ok}/{n} ETFs fetched, {len(profiles)} profiles, "
                      f"{len(errors)} error(s).")
    return profiles, errors
