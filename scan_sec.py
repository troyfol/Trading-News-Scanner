# ==============================================================================
# Morning Scanner (Ultimate Edition II) - TradeStation + Full Settings Persistence
# ==============================================================================

import sys
import os
import time
import json
import logging
import math
import tempfile
import threading
from collections import OrderedDict
from functools import lru_cache
import tkinter as tk
from tkinter import ttk, font
from datetime import datetime, timedelta, timezone
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover — zoneinfo always present on 3.10+
    _ET_TZ = None


def _now_et():
    """Current wall-clock time in US/Eastern (the market timezone the
    wires UI is anchored to). Falls back to naive local time only if
    zoneinfo is unavailable."""
    return datetime.now(_ET_TZ) if _ET_TZ is not None else datetime.now()


def _fmt_signed_pct(v):
    """Format a percent value as e.g. ``+5.34%``, or None when ``v`` is
    None or non-finite (NaN/inf). Guards the surprise/YoY label boundary
    so a bad upstream value renders as 'no data' rather than the literal
    ``+nan%`` on a magnitude-conveying chart/row."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f"{f:+.2f}%"
import re
import html
import requests
from bs4 import BeautifulSoup
import feedparser
import concurrent.futures
import webbrowser
from urllib.parse import quote as url_quote, urlparse

# rapidfuzz is significantly faster than difflib for the CIK name
# match. If the wheel isn't available (very old environments), fall
# back to difflib so the resolver still works — just slower.
try:
    from rapidfuzz import fuzz as _rf_fuzz, process as _rf_process
    _HAS_RAPIDFUZZ = True
except ImportError:
    import difflib as _difflib
    _HAS_RAPIDFUZZ = False

try:
    import win32gui
except ImportError:
    print("ERROR: pywin32 is required. Install with: pip install pywin32")
    try:
        import tkinter.messagebox as _mb
        _r = tk.Tk(); _r.withdraw()
        _mb.showerror("Missing Dependency", "pywin32 is required.\nInstall with: pip install pywin32")
        _r.destroy()
    except Exception:
        pass
    sys.exit(1)

# comtypes is only needed for TITAN X (UI Automation). If missing the
# TITAN mode silently becomes a no-op; the other modes still work.
try:
    import comtypes
    import comtypes.client
    _HAS_COMTYPES = True
except ImportError:
    _HAS_COMTYPES = False

# keyring stores the user's Polygon API key in Windows Credential
# Manager. If the package is missing the Historical lookup falls back
# to EDGAR-only mode (Polygon section silently skipped).
try:
    import keyring as _keyring
    _HAS_KEYRING = True
except ImportError:
    _HAS_KEYRING = False
KEYRING_SERVICE = "MorningScanner"
KEYRING_POLYGON_KEY = "polygon_api_key"

def _keyring_backend_is_secure():
    """True only when the active keyring backend is the encrypted
    Windows Credential Manager (``WinVaultKeyring``). Guards against
    keyring silently auto-selecting the ``fail`` or ``null`` backend
    (which would discard the key with no signal) if WinVaultKeyring
    fails to initialize in a frozen build. ``_log`` is resolved lazily
    so this stays import-safe even though it's defined below."""
    if not _HAS_KEYRING:
        return False
    try:
        from keyring.backends import Windows as _kr_win
        return isinstance(_keyring.get_keyring(), _kr_win.WinVaultKeyring)
    except Exception:
        return False

def _keyring_get_polygon():
    if not _HAS_KEYRING:
        return None
    try:
        v = _keyring.get_password(KEYRING_SERVICE, KEYRING_POLYGON_KEY)
        return v.strip() if v else None
    except Exception:
        return None

def _keyring_set_polygon(value):
    if not _HAS_KEYRING:
        return False
    if not _keyring_backend_is_secure():
        # Refuse to "store" into a fail/null backend that would silently
        # drop the key — surface it (the Settings dialog shows the False
        # as a save error) instead of pretending it persisted.
        try:
            _log.warning("keyring backend is not the secure Windows "
                         "Credential Manager (got %s); refusing to store "
                         "the Polygon key",
                         type(_keyring.get_keyring()).__name__)
        except Exception:
            pass
        return False
    try:
        _keyring.set_password(KEYRING_SERVICE, KEYRING_POLYGON_KEY, value)
        return True
    except Exception as exc:
        try:
            _log.warning("keyring set failed: %s", type(exc).__name__)
        except Exception:
            pass
        return False

def _keyring_clear_polygon():
    if not _HAS_KEYRING:
        return False
    try:
        _keyring.delete_password(KEYRING_SERVICE, KEYRING_POLYGON_KEY)
        return True
    except Exception as exc:
        # delete_password raises PasswordDeleteError when the key
        # doesn't exist — caller already treats False as "nothing to
        # clear" so this collapse is fine. Log at debug (never the key).
        try:
            _log.debug("keyring clear no-op/failed: %s", type(exc).__name__)
        except Exception:
            pass
        return False

# --- CONFIG ---
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

WIRE_CACHE_PATH = BASE_DIR / "wires_cache.json"
SEC_CACHE_PATH = BASE_DIR / "sec_tickers.json"
SETTINGS_FILE = BASE_DIR / "scanner_settings.json"

# Single-stock ETF map — JSON ships in the bundle and is also writable
# alongside the exe (same pattern as the other caches above). Custom
# path can be set via Settings → ETF Map.
from etf_map import EtfMap, format_mult, DEFAULT_WRITABLE_PATH as ETF_MAP_DEFAULT_PATH
from etf_holdings import EtfHoldings

# SEC fair-access guidance asks for a real declared sample contact in
# the User-Agent. Read it from the MS_SEC_CONTACT env var when set;
# otherwise fall back to a non-deliverable placeholder (so the shipped
# exe carries no real PII) and warn once at startup that SEC may throttle
# the default. ``_SEC_CONTACT_IS_PLACEHOLDER`` drives that one-line warn
# in ScannerApp.__init__ (where the module logger is available).
_SEC_CONTACT_PLACEHOLDER = "admin@example.com"
_SEC_CONTACT = os.environ.get("MS_SEC_CONTACT", "").strip()
_SEC_CONTACT_IS_PLACEHOLDER = not _SEC_CONTACT
UA_LIST = [f"MorningScanner/1.0 ({_SEC_CONTACT or _SEC_CONTACT_PLACEHOLDER})"]
HEADERS = {"User-Agent": UA_LIST[0]}

# A real declared contact is preferred but not required. We accept the
# common "local@domain.tld" shape and reject obvious junk so a typo in
# the Settings field can't silently ship a malformed User-Agent. Blank is
# allowed (callers treat it as "use the env var / placeholder fallback").
_SEC_CONTACT_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _is_valid_sec_contact(s) -> bool:
    return bool(_SEC_CONTACT_RE.match((s or "").strip()))


def _set_sec_contact(contact) -> None:
    """Rebuild the SEC User-Agent globals from ``contact`` at runtime.

    Every SEC scraper reads ``HEADERS["User-Agent"]`` / ``UA_LIST[0]`` at
    call time, so mutating these dict/list entries IN PLACE (never
    rebinding the names) propagates a Settings-menu change to all future
    SEC requests with no restart. A blank/None contact falls back to the
    non-deliverable placeholder."""
    global _SEC_CONTACT, _SEC_CONTACT_IS_PLACEHOLDER
    contact = (contact or "").strip()
    _SEC_CONTACT = contact
    _SEC_CONTACT_IS_PLACEHOLDER = not contact
    ua = f"MorningScanner/1.0 ({contact or _SEC_CONTACT_PLACEHOLDER})"
    UA_LIST[0] = ua
    HEADERS["User-Agent"] = ua
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Referer": "https://finviz.com"
}
# RSS wire feeds get their own headers. The Finviz Referer above is
# meaningless to prnewswire/globenewswire/yahoo and measurably worsens
# PRNewswire's bot-filtering (it answers a fraction of requests with a
# 404 HTML page instead of the feed). Declaring an RSS Accept also nudges
# origins to serve XML rather than a browser landing page.
WIRE_HEADERS = {
    "User-Agent": BROWSER_HEADERS["User-Agent"],
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}
MIN_SCRAPE_INTERVAL = 1.0  # default; user-tunable via Settings dialog
MIN_SCRAPE_INTERVAL_RANGE = (0.1, 10.0)
MIN_SEC_INTERVAL = 0.15  # SEC fair-access: 10 req/s cap; stay well under

# Float coloration: shares-float below this many shares renders in the
# "low" color (a small float is the trade-relevant signal), else the
# "high" color. Both the cutoff and the two colors are user-tunable via
# the Settings dialog. The colors default to "" meaning "follow the
# theme's green/red", so the out-of-the-box look is unchanged.
LOW_FLOAT_DEFAULT = 20_000_000
LOW_FLOAT_RANGE_M = (0.1, 100_000.0)  # cutoff entered in MILLIONS of shares

# Market-cap stepped gradient. MCap is always shown in the header (in the
# large font Float used to occupy); when the gradient is enabled it's
# painted with one of five tier colors keyed by USD market cap. The ramp
# runs bright-red (micro) -> bright-green (mega) with a very-light-green
# midpoint. All five colors + the on/off toggle are user-tunable via the
# Settings dialog. Tier boundaries (USD):
#   micro  < 250M | small 250M-2B | mid 2B-10B | large 10B-200B | mega >=200B
MCAP_TIER_BOUNDS = (250_000_000, 2_000_000_000,
                    10_000_000_000, 200_000_000_000)
MCAP_TIER_KEYS = ("micro", "small", "mid", "large", "mega")
MCAP_TIER_DEFAULT_COLORS = {
    "micro": "#FF2B2B",  # bright red
    "small": "#FF9030",  # orange
    "mid":   "#CFF5C8",  # very light green
    "large": "#5FD35F",  # medium green
    "mega":  "#00C400",  # bright green
}
MCAP_TIER_LABELS = {
    "micro": "Micro (<$250M)",
    "small": "Small ($250M-$2B)",
    "mid":   "Mid ($2B-$10B)",
    "large": "Large ($10B-$200B)",
    "mega":  "Mega ($200B+)",
}


def _parse_mcap_dollars(text):
    """Parse a finviz-style market-cap string ('1.50B', '850.00M',
    '12.3K', '1.2T') into a float number of US dollars, or None if it
    can't be parsed."""
    if not text:
        return None
    clean = str(text).strip().upper().replace("$", "").replace(",", "")
    if not clean:
        return None
    mult = 1.0
    if clean.endswith("T"): mult, clean = 1_000_000_000_000, clean[:-1]
    elif clean.endswith("B"): mult, clean = 1_000_000_000, clean[:-1]
    elif clean.endswith("M"): mult, clean = 1_000_000, clean[:-1]
    elif clean.endswith("K"): mult, clean = 1_000, clean[:-1]
    try:
        return float(clean) * mult
    except (ValueError, TypeError):
        return None


def _mcap_tier(dollars):
    """Return the tier key ('micro'..'mega') for a USD market cap, or
    None if ``dollars`` is None."""
    if dollars is None:
        return None
    b_micro, b_small, b_mid, b_large = MCAP_TIER_BOUNDS
    if dollars < b_micro: return "micro"
    if dollars < b_small: return "small"
    if dollars < b_mid: return "mid"
    if dollars < b_large: return "large"
    return "mega"

# Default location of the earnings-history parquet produced by an
# upstream earnings pipeline. It carries `yoy_eps_pct` + `yoy_rev_pct`
# columns the Historical Lookup uses for 10-Q YoY enrichment. The file
# is optional — the scanner runs fine without it (earnings YoY columns
# and the earnings chart simply stay empty). Point it at your own
# parquet via the Settings dialog; the default looks for one named
# ``earnings_history.parquet`` next to the app.
DEFAULT_EARNINGS_DB_PATH = str(BASE_DIR / "earnings_history.parquet")
# Older settings files may hold a prior path; any entry listed here is
# auto-migrated forward to ``DEFAULT_EARNINGS_DB_PATH`` on load so users
# don't have to re-point their parquet after a move. Custom paths (not
# in this set) are left alone. Empty by default.
_LEGACY_EARNINGS_DB_PATHS = ()


def _atomic_write_json(path, obj):
    """Write ``obj`` as JSON to ``path`` atomically: dump to a sibling
    temp file in the same directory, then ``os.replace`` it into place.
    A crash / kill / power-loss / disk-full mid-write can therefore never
    leave a truncated or empty file — the reader sees either the old
    contents or the new ones, never a partial. Mirrors the temp+replace
    idiom already used for the SEC and wires caches. Raises on failure;
    the caller decides whether to swallow."""
    path = Path(path)
    # Unique temp name via mkstemp (not a fixed ``*.tmp``) so a predictable
    # name in the shared-with-exe directory can't be pre-created/symlinked
    # by another local process, and a stale temp from a prior crash can't
    # collide.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup so a failed write doesn't leave a stale temp.
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise

# --- THEMES ---
THEMES = {
    "dark": {
        "BG": "#141211", "FG": "#F2EFED", "ACCENT": "#2B2B2B",
        "TXT_OK": "#00FF00", "TXT_BAD": "#FF4444", "HIGHLIGHT": "#00D7FF",
        "CREDIT": "#888888", "VIOLET": "#D699FF", "STATUS_WAIT": "#555555",
        "STATUS_OK": "#00FF00", "STATUS_ERR": "#FF4444",
        # Amber for the symbol-stall indicator (watch thread blocked >8s
        # inside its active-window get_info() call — typically a TITAN
        # UIA hang). Visible warning that the displayed symbol may be
        # stale.
        "STATUS_STALL": "#FFA500",
        "ENTRY_BG": "#333333", "ENTRY_FG": "white", "BTN_BG": "#444444", "BTN_FG": "white",
        "TREE_SEL": "#444444", "TREE_OLD": "#888888",
        "SEC_HOT": "#00FF00", "SEC_WARM": "#66FF66", "SEC_COLD": "#888888",
        "EARN_FUTURE": "#FFE600",
        "ETF_BLUE": "#4FB3FF"
    },
    "light": {
        "BG": "#FFFFFF", "FG": "#000000", "ACCENT": "#E0E0E0",
        "TXT_OK": "#00AA00", "TXT_BAD": "#CC0000", "HIGHLIGHT": "#0000FF",
        "CREDIT": "#888888", "VIOLET": "#8A2BE2", "STATUS_WAIT": "#CCCCCC",
        "STATUS_OK": "#00FF00", "STATUS_ERR": "#FF4444",
        "STATUS_STALL": "#CC6600",
        "ENTRY_BG": "#EEEEEE", "ENTRY_FG": "black", "BTN_BG": "#DDDDDD", "BTN_FG": "black",
        "TREE_SEL": "#CCCCCC", "TREE_OLD": "#888888",
        "SEC_HOT": "#00AA00", "SEC_WARM": "#66CC66", "SEC_COLD": "#888888",
        "EARN_FUTURE": "#CC9900",
        "ETF_BLUE": "#0066CC"
    }
}

# ==============================================================================
# CIK RESOLVER
# ==============================================================================
class CIKResolver:
    def __init__(self):
        self.ticker_map = {}
        self.name_map = {}
        self._prefix_map = {}
        # Reverse name->ticker index (for resolving ETF swap descriptions
        # like "ROCKET LAB CORPORATION-SWAP-..." back to RKLB). Separate
        # from the CIK structures above so that path is untouched.
        self._name_ticker_map = {}
        self._nt_prefix = {}
        self.loaded = False
        self._lock = threading.Lock()
        # Dedicated session for the SEC ticker manifest pull. Owned by
        # the resolver so close() can shut it down cleanly (R9).
        self._session = requests.Session()
        self._refresh_thread = threading.Thread(
            target=self.refresh_sec_list, daemon=True, name="MS-CIKRefresh",
        )
        self.load_local_cache()
        self._refresh_thread.start()

    def close(self):
        # Give the refresh thread a brief moment to finish so its
        # session.get() can unwind cleanly. Daemon = True means we
        # don't block app exit indefinitely if the SEC fetch is wedged
        # — 1s is the same timeout used by other thread joins in the
        # app.
        try:
            self._refresh_thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            self._session.close()
        except Exception:
            pass

    def normalize_name(self, name):
        if not name: return ""
        n = name.upper()
        n = re.sub(r'[^A-Z0-9\s]', '', n) 
        remove_words = ["INC", "CORP", "CORPORATION", "LTD", "LIMITED", "LLC", "COMPANY", "PLC"]
        tokens = [t for t in n.split() if t not in remove_words]
        return " ".join(tokens)

    def load_local_cache(self):
        if SEC_CACHE_PATH.exists():
            try:
                with open(SEC_CACHE_PATH, "r") as f:
                    data = json.load(f)
                    self.process_data(data)
            except (json.JSONDecodeError, OSError, KeyError, ValueError,
                    AttributeError, TypeError):
                pass

    def refresh_sec_list(self):
        url = "https://www.sec.gov/files/company_tickers.json"
        try:
            # Hard 15s timeout; uses the resolver's own session so
            # close() can interrupt long-tail connection teardown.
            # stream=True + _read_capped bounds the manifest (~1-2 MB
            # legitimately) so a hostile/MITM origin can't balloon r.json().
            r = self._session.get(url, headers=HEADERS, timeout=15, stream=True)
            if r.status_code == 200:
                raw = _read_capped(r, _HTTP_MAX_BYTES_SEC_JSON)
                data = json.loads(raw)
                self.process_data(data)
                # Atomic + unique-temp write (shared helper) — replaces the
                # old fixed-name ``SEC_CACHE_PATH.with_suffix('.tmp')``.
                _atomic_write_json(SEC_CACHE_PATH, data)
        except (requests.RequestException, json.JSONDecodeError, OSError,
                KeyError, ValueError, AttributeError, TypeError):
            pass

    def process_data(self, raw_json):
        t_map = {}
        n_map = {}
        prefix_map = {}  # first char -> list of (norm_name, cik)
        nt_map = {}      # norm_name -> ticker  (reverse, for swap resolution)
        nt_prefix = {}   # first char -> list of (norm_name, ticker)
        if not isinstance(raw_json, dict):
            # A tampered/garbage manifest that still parses as JSON but
            # isn't the expected ``{idx: {cik_str, ticker, title}}`` shape
            # must not crash startup — this runs synchronously in __init__.
            return
        for k, v in raw_json.items():
            if not isinstance(v, dict):
                continue
            cik_raw = v.get("cik_str")
            if cik_raw is None:
                # Skip rather than producing a bogus "000000None" CIK.
                continue
            cik = str(cik_raw).zfill(10)
            tick = str(v.get("ticker", "")).upper()
            title = str(v.get("title", "") or "")
            t_map[tick] = {"cik": cik, "title": title}
            norm_title = self.normalize_name(title)
            if norm_title:
                n_map[norm_title] = cik
                ch = norm_title[0]
                if ch not in prefix_map:
                    prefix_map[ch] = []
                prefix_map[ch].append((norm_title, cik))
                # Reverse index (name -> ticker). Skip blank tickers so a
                # company with no listed symbol can't shadow a real one.
                if tick:
                    nt_map.setdefault(norm_title, tick)
                    nt_prefix.setdefault(ch, []).append((norm_title, tick))
        with self._lock:
            self.ticker_map = t_map
            self.name_map = n_map
            self._prefix_map = prefix_map
            self._name_ticker_map = nt_map
            self._nt_prefix = nt_prefix
            self.loaded = True

    def resolve_name_to_ticker(self, name, min_ratio=0.90):
        """Best-effort company-name -> ticker via the SEC manifest. Used to
        turn an ETF swap description ("ROCKET LAB CORPORATION-SWAP-...")
        into a real symbol (RKLB). Exact normalized-title match first, then
        a conservative fuzzy match within the same first-letter bucket;
        returns None when nothing clears ``min_ratio`` (we'd rather omit a
        holding than mis-resolve it)."""
        norm = self.normalize_name(name)
        if not norm:
            return None
        with self._lock:
            exact = self._name_ticker_map.get(norm)
            if exact:
                return exact
            candidates = list(self._nt_prefix.get(norm[0], []))
        best, best_r = None, 0.0
        for cand_norm, tick in candidates:
            r = self._ratio(norm, cand_norm)
            if r > best_r:
                best_r, best = r, tick
        return best if best_r >= min_ratio else None

    @staticmethod
    def _ratio(a, b):
        """Normalized similarity ratio in [0.0, 1.0]. Uses rapidfuzz
        when available (~50× faster than difflib on the candidates we
        scan); falls back to difflib otherwise."""
        if _HAS_RAPIDFUZZ:
            return _rf_fuzz.ratio(a, b) / 100.0
        return _difflib.SequenceMatcher(None, a, b).ratio()

    def get_cik(self, symbol, window_title_name=None):
        clean_sym = symbol.upper().strip()
        with self._lock:
            t_map = self.ticker_map
            n_map = self.name_map
            p_map = self._prefix_map
        found = t_map.get(clean_sym)
        if found:
            if window_title_name:
                sec_name = found['title']
                norm_sec = self.normalize_name(sec_name)
                norm_win = self.normalize_name(window_title_name)
                sec_words = norm_sec.split()
                win_words = norm_win.split()
                if sec_words and win_words:
                    if self._ratio(norm_sec, norm_win) > 0.4:
                        return found['cik']
                    # else fall through to fuzzy / fallback paths
                else:
                    return found['cik']
        if window_title_name:
            norm_win = self.normalize_name(window_title_name)
            if not norm_win:
                if found:
                    return found['cik']
                return None
            if norm_win in n_map:
                return n_map[norm_win]
            candidates = p_map.get(norm_win[0], [])
            if not candidates:
                if found:
                    return found['cik']
                return None
            if _HAS_RAPIDFUZZ:
                # ``process.extractOne`` runs the full sweep in C with
                # an internal cutoff — saves us the Python-level loop.
                names = [c[0] for c in candidates]
                best = _rf_process.extractOne(
                    norm_win, names, scorer=_rf_fuzz.ratio, score_cutoff=60.0,
                )
                if best is not None:
                    _, score, idx = best
                    return candidates[idx][1]
            else:
                best_match = None
                best_ratio = 0.0
                for sec_name, cik in candidates:
                    ratio = self._ratio(sec_name, norm_win)
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_match = cik
                if best_ratio > 0.6:
                    return best_match
        if found:
            return found['cik']
        return None

# ==============================================================================
# INTERNAL RSS WORKER
# ==============================================================================
class RSSWorker:
    # Minimum seconds between successive ``fetch_feeds`` calls. Manual
    # refresh and the 60s loop both pull through ``fetch_feeds``; this
    # gate keeps two near-simultaneous fires from re-pulling the same
    # remote feeds (E7).
    MIN_FETCH_INTERVAL = 20.0
    # Wire origins (PRNewswire especially) bot-filter a fraction of
    # requests, answering 404/301 with an HTML page instead of the feed.
    # Those failures are independent, so a couple of quick retries take
    # the effective success rate back to ~100%.
    WIRE_ATTEMPTS = 3
    WIRE_RETRY_BACKOFF = 0.6  # seconds between attempts
    # Consecutive failed cycles before a wire indicator turns red. One
    # transient miss (already rare once retries are in) shouldn't flash
    # the indicator red for a whole 60s cycle.
    FAIL_STREAK_TO_ERR = 2

    def __init__(self):
        self.running = False
        self.feeds = [
            ("GB", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies"),
            ("PR", "https://www.prnewswire.com/rss/news-releases-list.rss"),
            ("YH", "https://finance.yahoo.com/news/rssindex")
        ]
        self.statuses = {code: None for code, url in self.feeds}
        # Consecutive-failure count per feed, driving the ERR hysteresis
        # in ``_apply_status``. Any success resets it immediately.
        self._fail_streaks = {code: 0 for code, url in self.feeds}
        # Serialize cache read-modify-write between the 60s loop and any
        # manual refresh (C3). Independent of the fetch dedupe lock.
        self._cache_lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._last_fetch_at = 0.0
        self._last_fetch_items: list = []
        # In-memory mirror of the wires cache (E2). Populated lazily by
        # ``get_items`` and refreshed atomically inside merge_into_cache
        # (under ``_cache_lock``). Eliminates the disk read each
        # ``DataFetcher.get_wires`` call would otherwise do.
        self._items_mirror: list = []
        self._items_loaded = False
    
    _SOURCE_LABELS = {"YH": "Yahoo", "PR": "PRNew", "GB": "Globe"}

    def _fetch_one(self, code, url):
        """Fetch and parse a single feed. Returns (code, status, items)."""
        source = self._SOURCE_LABELS.get(code, "Wire")
        # Retry before giving up: wire origins bot-filter an occasional
        # request (see WIRE_ATTEMPTS). Failures are logged with the feed
        # code + reason only — never the URL.
        raw = None
        last_why = ""
        for attempt in range(1, self.WIRE_ATTEMPTS + 1):
            try:
                # stream=True + _read_capped: bound the RSS body so a hostile/
                # compromised feed origin (or a TLS-MITM) can't balloon-load
                # the always-on RSS daemon's memory every 60s.
                r = requests.get(url, headers=WIRE_HEADERS, timeout=10,
                                 stream=True)
                if r.status_code == 200:
                    raw = _read_capped(r, _HTTP_MAX_BYTES_SCRAPE_HTML)
                    break
                last_why = "HTTP %d" % r.status_code
            except (requests.RequestException, OSError, ValueError) as exc:
                last_why = type(exc).__name__
            if attempt < self.WIRE_ATTEMPTS:
                time.sleep(self.WIRE_RETRY_BACKOFF)
        if raw is None:
            _log.warning("RSS %s: fetch failed after %d attempts (%s)",
                         code, self.WIRE_ATTEMPTS, last_why)
            return code, "ERR", []

        try:
            # ``sanitize_html`` defaults to True in feedparser, but make
            # it explicit so a future version flip or local override can't
            # silently strip the protection. Crafted RSS items can carry
            # `<script>` / event-handler attrs in titles + descriptions;
            # the sanitizer drops them before they reach our HTML unescape
            # + Treeview render path.
            parsed = feedparser.parse(raw, sanitize_html=True)
        except Exception as exc:
            _log.warning("RSS %s: parse failed (%s)", code, type(exc).__name__)
            return code, "ERR", []

        out = []
        # Anchor both the parsed-pubdate path and the fallback to ET so
        # the stored date/time share a basis with the 7-day cutoff and
        # the today_iso "is_today" comparison (both ET). Previously the
        # parsed branch formatted feedparser's UTC struct as if local,
        # mis-dating near-midnight wires by the 4-5h offset.
        now_dt = _now_et()
        fallback_iso = now_dt.date().isoformat()
        fallback_t = now_dt.strftime("%I:%M%p")
        for entry in parsed.entries:
            # One malformed entry must not sink the whole feed. Before this
            # guard an unexpected error here escaped _fetch_one entirely,
            # and fetch_feeds could not attribute it to a wire, so that
            # feed's status stayed frozen at its previous value.
            try:
                title = (entry.get("title") or "").strip()
                if not title:
                    continue
                title = html.unescape(title)
                link = html.unescape((entry.get("link") or "").strip())
                # feedparser surfaces a parsed time tuple as ``published_parsed``
                # (or ``updated_parsed``). When present, it represents the
                # publisher's pubDate — exactly what we want.
                t_struct = entry.get("published_parsed") or entry.get("updated_parsed")
                if t_struct:
                    try:
                        # feedparser normalizes published_parsed to UTC; build
                        # a UTC-aware datetime and convert to ET so it matches
                        # the cutoff/today_iso basis.
                        pub_dt = datetime(*t_struct[:6], tzinfo=timezone.utc)
                        if _ET_TZ is not None:
                            pub_dt = pub_dt.astimezone(_ET_TZ)
                        date_iso = pub_dt.date().isoformat()
                        time_str = pub_dt.strftime("%I:%M%p")
                    except (ValueError, TypeError):
                        date_iso, time_str = fallback_iso, fallback_t
                else:
                    date_iso, time_str = fallback_iso, fallback_t
                out.append({
                    "source": source, "headline": title, "url": link,
                    "time": time_str, "date": date_iso, "tickers": [],
                })
            except Exception:
                continue
        return code, "OK", out

    def _apply_status(self, code, status):
        """Map one cycle's outcome to the displayed status, with
        hysteresis. A single transient failure keeps the previous status
        (the retries in ``_fetch_one`` already absorb most blips); only
        ``FAIL_STREAK_TO_ERR`` consecutive failed cycles turn the
        indicator red. Any success resets the streak immediately."""
        if status == "OK":
            self._fail_streaks[code] = 0
            self.statuses[code] = "OK"
            return
        streak = self._fail_streaks.get(code, 0) + 1
        self._fail_streaks[code] = streak
        if streak >= self.FAIL_STREAK_TO_ERR:
            self.statuses[code] = "ERR"

    def fetch_feeds(self, report_dedupe=False):
        """Pull all wires. Returns the merged item list, or
        ``(items, was_deduped)`` when ``report_dedupe`` is set.

        ``was_deduped`` is True when the call was served from the
        MIN_FETCH_INTERVAL window: the origins were NOT contacted and
        ``self.statuses`` was NOT updated. The manual-refresh path uses
        it to say so, rather than silently doing nothing while stamping a
        fresh "Last Refreshed" time."""
        # Three feeds run in parallel — they're independent network calls
        # bottlenecked on origin latency, not CPU.
        # Dedupe between near-simultaneous callers (60s loop + manual
        # refresh): if a fetch happened within MIN_FETCH_INTERVAL,
        # return the cached result instead of re-hitting origins.
        with self._fetch_lock:
            now_t = time.time()
            if now_t - self._last_fetch_at < self.MIN_FETCH_INTERVAL:
                cached = list(self._last_fetch_items)
                # Flag travels with the return value, so it can't be
                # clobbered by the 60s loop racing the manual caller.
                return (cached, True) if report_dedupe else cached
            self._last_fetch_at = now_t

        items = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.feeds)) as ex:
            # Map future -> feed code so a worker that raises can still be
            # attributed. Previously this was a bare list: an exception out
            # of _fetch_one was skipped without touching statuses, leaving
            # that wire frozen at its old value (a broken feed could keep
            # reading green).
            future_to_code = {ex.submit(self._fetch_one, code, url): code
                              for code, url in self.feeds}
            for fut in concurrent.futures.as_completed(future_to_code):
                code = future_to_code[fut]
                try:
                    _code, status, feed_items = fut.result()
                except Exception as exc:
                    _log.warning("RSS %s: worker crashed (%s)",
                                 code, type(exc).__name__)
                    self._apply_status(code, "ERR")
                    continue
                self._apply_status(code, status)
                items.extend(feed_items)
        with self._fetch_lock:
            self._last_fetch_items = list(items)
        return (items, False) if report_dedupe else items

    def merge_into_cache(self, new_items):
        """Atomic read-modify-write of the wires cache. Both the 60s
        loop and manual refresh route through this — the lock prevents
        a partial merge produced by one caller from being clobbered by
        the other (C3). Returns the merged list."""
        with self._cache_lock:
            if self._items_loaded:
                cached = list(self._items_mirror)
            else:
                cached = self.load_cache()
            seen = set(i.get("url") for i in cached if i.get("url"))
            merged = list(cached)
            count_new = 0
            for it in new_items:
                u = it.get("url")
                if u and u not in seen:
                    merged.insert(0, it)
                    seen.add(u)
                    count_new += 1
            cutoff = (_now_et() - timedelta(days=7)).date().isoformat()
            merged = [m for m in merged if m.get("date", "") >= cutoff]
            merged = merged[:500]
            if count_new > 0 or not cached:
                self.save_cache(merged)
            self._items_mirror = merged
            self._items_loaded = True
            return merged

    def get_items_snapshot(self):
        """In-memory snapshot of the wires cache for read-only consumers.
        Loads from disk once, then serves all subsequent reads from
        memory until ``merge_into_cache`` next runs (E2)."""
        with self._cache_lock:
            if not self._items_loaded:
                self._items_mirror = self.load_cache()
                self._items_loaded = True
            return list(self._items_mirror)

    def load_cache(self):
        if WIRE_CACHE_PATH.exists():
            try:
                with open(WIRE_CACHE_PATH, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                items = raw.get("items", []) if isinstance(raw, dict) else []
                # Drop any non-dict elements: a tampered/corrupt cache
                # element would otherwise make merge_into_cache's
                # ``i.get(...)`` raise AttributeError and kill the RSS
                # daemon thread for the whole session.
                return [i for i in items if isinstance(i, dict)]
            except (json.JSONDecodeError, OSError, KeyError,
                    AttributeError, TypeError):
                pass
        return []

    def save_cache(self, items):
        try:
            temp = WIRE_CACHE_PATH.with_suffix(".tmp")
            with open(temp, "w", encoding="utf-8") as f: json.dump({"items": items}, f, indent=2)
            os.replace(temp, WIRE_CACHE_PATH)
        except (OSError, TypeError):
            pass

    def run_loop(self):
        self.running = True
        while self.running:
            try:
                new_items = self.fetch_feeds()
                self.merge_into_cache(new_items)
            except (requests.RequestException, OSError, KeyError, TypeError,
                    ValueError, AttributeError) as e:
                # Log only the exception type — full ``e`` repr can carry
                # the requested URL (publisher RSS endpoints) and may
                # land in console screenshots.
                _log.warning("RSS loop error: %s", type(e).__name__)
            time.sleep(60)

    def start(self):
        t = threading.Thread(target=self.run_loop, daemon=True)
        t.start()

# ==============================================================================
# FINVIZ ty=ea EARNINGS PAGE — live per-quarter actuals
# ==============================================================================
# The quote.ashx?ty=ea page embeds an ``earningsData`` JSON array (one
# object per fiscal quarter). We use it to gap-fill the LOCAL parquet
# when a quarter post-dates the parquet's last refresh (the just-reported
# quarter), and to backfill YoY %s the parquet is missing. Conventions
# mirror earnings_pipeline.finviz_fill so synthesized rows match the
# parquet schema exactly: adjusted ``epsActual``/``salesActual`` (not the
# GAAP ``*Reported*`` fields), ``period_ending`` = day-1 of the
# fiscal-quarter-end month, surprise = (actual - estimate)/|estimate|.

_FV_EA_KEY = '"earningsData":'

# Small-base YoY floor — mirrors earnings_pipeline config.MIN_YOY_*_BASE and
# ScannerApp._YOY_SMALL_BASE_* (same 0.05 / 1.0 thresholds). A YoY % built on
# a prior-year base smaller than this is a tiny-denominator blowup (e.g. a
# -$0.01 -> +$0.04 swing reads as +500%), so we leave it NaN instead of
# computing it — matching the parquet's compute_yoy_columns, which now nulls
# these rather than greying them. Reported revenue is $M in the finviz ty=ea
# canonical rows ($1.0M == 1.0); EDGAR XBRL revenue facts are raw $ (== 1e6).
_YOY_MIN_BASE_EPS = 0.05             # $/share
_YOY_MIN_BASE_REV_M = 1.0            # $M  (finviz ty=ea canonical rows)
_YOY_MIN_BASE_REV_RAW = 1_000_000.0  # $   (EDGAR XBRL period facts)
# Dollars -> $millions divisor for XBRL revenue facts. A DISTINCT
# constant from _YOY_MIN_BASE_REV_RAW above (which happens to share the
# 1e6 literal but is a YoY base-floor THRESHOLD, not a unit conversion —
# conflating them would couple two unrelated knobs).
_XBRL_DOLLARS_PER_MILLION = 1_000_000.0


def _fv_ea_extract(html_text):
    """Pull the ``earningsData`` JSON array out of a finviz ty=ea page.
    Returns the parsed list (possibly empty), or None when the key is
    absent / unparseable. Uses ``raw_decode`` so a ``]`` inside a string
    value can't truncate the array (mirrors finviz_client._extract)."""
    if not html_text:
        return None
    i = html_text.find(_FV_EA_KEY)
    if i == -1:
        return None
    start = html_text.find("[", i)
    if start == -1:
        return None
    try:
        parsed, _end = json.JSONDecoder().raw_decode(html_text, start)
    except (ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, list) else None


def _fv_ea_to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fv_ea_report_time(hour):
    """Finviz earningsDate hour -> report_time bucket (>=16 Close,
    0<h<12 Open, else Unknown). Matches earnings_pipeline.finviz_fill."""
    if hour is None:
        return "Unknown"
    if hour >= 16:
        return "Close"
    if 0 < hour < 12:
        return "Open"
    return "Unknown"


def _fv_ea_row_from_entry(entry, sym):
    """Translate one finviz ``earningsData`` entry into a canonical
    earnings_history row dict (adjusted basis), or None for a forward
    estimate / malformed entry. YoY is left NaN here — filled by
    ``_fv_ea_rows_with_yoy``. Mirrors
    earnings_pipeline.finviz_fill._record_to_history_dict."""
    import pandas as pd
    if not isinstance(entry, dict):
        return None
    eps_actual = _fv_ea_to_float(entry.get("epsActual"))
    earnings_date = entry.get("earningsDate")
    fiscal_end = entry.get("fiscalEndDate")
    # Past quarters only — a real row carries an actual EPS + both dates.
    if eps_actual is None or not earnings_date or not fiscal_end:
        return None
    period_ts = pd.to_datetime(fiscal_end, errors="coerce")
    report_ts = pd.to_datetime(earnings_date, errors="coerce")
    if pd.isna(period_ts) or pd.isna(report_ts):
        return None
    eps_est = _fv_ea_to_float(entry.get("epsEstimate"))
    sales_actual = _fv_ea_to_float(entry.get("salesActual"))
    sales_est = _fv_ea_to_float(entry.get("salesEstimate"))
    surprise_eps_pct = None
    if eps_est is not None and abs(eps_est) > 0:
        surprise_eps_pct = (eps_actual - eps_est) / abs(eps_est) * 100.0
    surprise_rev_pct = None
    if (sales_actual is not None and sales_est is not None
            and abs(sales_est) > 0):
        surprise_rev_pct = (sales_actual - sales_est) / abs(sales_est) * 100.0
    return {
        "ticker": (sym or "").upper().strip(),
        # period_ending: day-1 of the fiscal-quarter-end month — the
        # same cross-source key the parquet uses, so (year, month)
        # matches the parquet's row for the same fiscal quarter.
        "period_ending": period_ts.replace(day=1).normalize(),
        "report_date": report_ts.normalize(),
        "report_time": _fv_ea_report_time(int(report_ts.hour)),
        "estimated_eps": eps_est,
        "reported_eps": eps_actual,
        "surprise_eps_pct": surprise_eps_pct,
        "estimated_rev": sales_est,
        "reported_rev": sales_actual,
        "surprise_rev_pct": surprise_rev_pct,
        "yoy_eps_pct": float("nan"),
        "yoy_rev_pct": float("nan"),
        "source": "finviz",
        "report_date_proxy": False,
    }


def _fv_ea_rows_with_yoy(entries, sym):
    """Build canonical rows from a finviz ``earningsData`` array, with
    ``yoy_eps_pct`` / ``yoy_rev_pct`` computed against the same-quarter
    prior-year row (period_ending month, one year back) — the same
    formula the parquet's compute_yoy_columns uses: (cur-base)/|base|.
    Returns rows sorted oldest -> newest by period_ending."""
    import pandas as pd
    rows = []
    for e in (entries or []):
        r = _fv_ea_row_from_entry(e, sym)
        if r is not None:
            rows.append(r)
    if not rows:
        return rows
    # One row per period_ending (keep the latest report_date if finviz
    # ever repeats a fiscal quarter).
    by_period = {}
    for r in rows:
        p = r["period_ending"]
        prior = by_period.get(p)
        if prior is None or r["report_date"] >= prior["report_date"]:
            by_period[p] = r
    rows = list(by_period.values())
    by_ym = {}
    for r in rows:
        pe = pd.Timestamp(r["period_ending"])
        by_ym[(pe.year, pe.month)] = r
    for r in rows:
        pe = pd.Timestamp(r["period_ending"])
        base = by_ym.get((pe.year - 1, pe.month))
        if base is None:
            continue
        be, br = base.get("reported_eps"), base.get("reported_rev")
        ce, cr = r.get("reported_eps"), r.get("reported_rev")
        # Small-base floor (== the parquet's policy): leave NaN rather than
        # emit a tiny-denominator blowup the parquet would have nulled.
        if be is not None and abs(be) >= _YOY_MIN_BASE_EPS and ce is not None:
            r["yoy_eps_pct"] = (ce - be) / abs(be) * 100.0
        if br is not None and abs(br) >= _YOY_MIN_BASE_REV_M and cr is not None:
            r["yoy_rev_pct"] = (cr - br) / abs(br) * 100.0
    rows.sort(key=lambda rr: pd.Timestamp(rr["period_ending"]))
    return rows


def _parse_eps_sales_surpr_cell(cell):
    """Parse a finviz snapshot 'EPS/Sales Surpr.' cell into
    ``(eps_display, sales_display)`` — each ``'+N.NN%'`` / ``'-N.NN%'`` or
    ``None`` when that slot is N/A.

    The cell holds two ORDERED slots, EPS then Sales. Each slot is either a
    value ``<span>`` (the sign lives in the span text, mirrored by an
    ``is-negative`` / ``is-positive`` class) or a bare ``-`` / ``—`` token
    (outside any span) meaning N/A. Observed live layouts this handles:
        both present  : <span>3.30%</span> <span>1.58%</span>      (AAPL)
        EPS neg       : <span is-negative>-2.91%</span> <span>0.92%</span> (CAG)
        Sales neg     : <span>8.14%</span> <span is-negative>-0.09%</span> (MMM)
        EPS N/A       : - <span>10.43%</span>                       (BBCP)
        Sales N/A     : <span>87.50%</span> -                       (GME)

    The legacy regex collapsed the BBCP case ``'- 10.43%'`` into a single
    ``'-10.43%'`` and mis-assigned it to EPS with a flipped sign — surfacing
    a phantom EPS *miss* when the truth was an EPS N/A + a Sales *beat*.
    Reading the slots in document order off the spans' shared parent fixes
    that and stays robust to nesting changes."""
    if cell is None:
        return None, None
    value_spans = [
        s for s in cell.find_all("span")
        if any(("is-positive" in c) or ("is-negative" in c)
               for c in (s.get("class") or []))
    ]
    host = value_spans[0].parent if value_spans else (cell.find("small") or cell)
    slots = []  # ordered; each entry is a '+/-N%' string or None (N/A)
    for node in getattr(host, "children", []):
        if getattr(node, "name", None) == "span":
            txt = node.get_text(strip=True)
            m = re.search(r"\d[\d.,]*", txt)
            if not m:
                continue
            num = m.group(0).replace(",", "")
            cls = " ".join(node.get("class") or [])
            neg = ("is-negative" in cls) or txt.lstrip().startswith("-")
            slots.append(("-" if neg else "+") + num + "%")
        else:
            s = node.get_text() if hasattr(node, "get_text") else str(node)
            for tok in re.findall(r"-?\d[\d.,]*%|[-—]", s):
                if tok in ("-", "—"):
                    slots.append(None)  # N/A placeholder
                else:
                    v = tok.replace(",", "")
                    slots.append(v if v.startswith("-") else "+" + v)
    eps = slots[0] if len(slots) >= 1 else None
    sales = slots[1] if len(slots) >= 2 else None
    return eps, sales

# ==============================================================================
# DATA FETCHER
# ==============================================================================
class DataFetcher:
    def __init__(self):
        self.session = requests.Session()
        self._scrape_lock = threading.Lock()
        self.last_scrape_time = 0
        self._sec_lock = threading.Lock()
        self.last_sec_time = 0
        self.finviz_status = None
        self.sec_status = None
        # Per-fetcher throttle (settings-tunable) — defaults to the
        # module constant but can be overridden at runtime via the
        # Settings dialog.
        self.finviz_min_interval = MIN_SCRAPE_INTERVAL
        # Low-float cutoff (shares). Settings-tunable; drives parse_float's
        # is_low flag and therefore the float label's color.
        self.float_low_threshold = LOW_FLOAT_DEFAULT
        # Per-session cache for the finviz ty=ea earnings page (sym ->
        # list of canonical rows with YoY, or [] on miss). Avoids a
        # repeat scrape when the same symbol's chart is reopened or the
        # landing-row backfill re-fires. Negatives are cached too.
        self._ea_cache: "OrderedDict[str, list]" = OrderedDict()
        self._ea_cache_lock = threading.Lock()
        self.rss_worker = RSSWorker()
        self.rss_worker.start()
        self.cik_resolver = CIKResolver()
        # Long-lived pool reused for the parallel Finviz+SEC fan-out
        # and any other short network jobs (E4). Avoids the per-fetch
        # ThreadPoolExecutor build/teardown cost that previously fired
        # on every symbol change.
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="MS-Fetch",
        )
        # Per-session submissions cache, keyed by padded CIK. Used by
        # scrape_sec_data AND list_earnings_filings so we don't refetch
        # the same submissions JSON twice per ticker (it's ~50–500 KB
        # per call and the data never changes within a session).
        self._submissions_cache: "OrderedDict[str, dict]" = OrderedDict()
        self._submissions_cache_lock = threading.Lock()

    # LRU caps for the two per-session caches above. The submissions
    # blobs are 50-500 KB each, so an all-day scan touching thousands of
    # distinct tickers would otherwise grow unbounded; cap and evict
    # oldest (matching the _xbrl_facts_cache / _oneliner_cache pattern).
    _EA_CACHE_MAX = 512
    _SUBMISSIONS_CACHE_MAX = 256

    @staticmethod
    def _evict_lru(cache, max_len):
        """Pop oldest entries from an OrderedDict until it is within
        ``max_len``. Caller holds the cache's lock."""
        while len(cache) > max_len:
            cache.popitem(last=False)

    def submit(self, fn, *args, **kwargs):
        """Schedule a function on the shared fetch pool."""
        return self._executor.submit(fn, *args, **kwargs)

    def close(self):
        self.rss_worker.running = False
        try:
            self.session.close()
        except Exception:
            pass
        try:
            self.cik_resolver.close()
        except Exception:
            pass
        try:
            # Don't block app close on stragglers — tasks queued at
            # shutdown will be cancelled or simply die with the process.
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def get_time_ago(self, date_obj, time_str):
        if not time_str or date_obj != datetime.now().date(): return ""
        try:
            now = datetime.now()
            dt_time = datetime.strptime(time_str, "%I:%M%p").time()
            dt_news = datetime.combine(now.date(), dt_time)
            diff = (now - dt_news).total_seconds()
            if diff < 60: return "now"
            mins = int(diff / 60)
            if mins < 60: return f"{mins}m"
            return f"{int(mins/60)}h"
        except (ValueError, TypeError, AttributeError):
            return ""

    def parse_float(self, text):
        if not text: return text, False
        clean = text.upper().strip()
        try:
            val = 0.0
            if clean.endswith("M"): val = float(clean[:-1]) * 1_000_000
            elif clean.endswith("B"): val = float(clean[:-1]) * 1_000_000_000
            elif clean.endswith("K"): val = float(clean[:-1]) * 1_000
            else: val = float(clean)
            return text, (val < self.float_low_threshold)
        except (ValueError, TypeError):
            return text, False

    def get_wires(self, symbol):
        items = []
        # Pull from the worker's in-memory mirror — no disk read on
        # the hot path (E2). The worker still serializes against its
        # own merge writes via _cache_lock, so what we get back is
        # always a consistent snapshot.
        raw_list = self.rss_worker.get_items_snapshot()
        if not raw_list:
            return items
        try:
            today_iso = _now_et().date().isoformat()

            clean_sym = symbol.upper().strip()
            # Either an explicit anchor (`$TICK`, `EXCH: TICK`, `(TICK)`,
            # `(NASDAQ: TICK)`) OR a case-sensitive whole-word ALL-CAPS
            # match. Case-sensitive avoids the prior false positives
            # ("MARK" matching "Marketing", "EYES" matching "All eyes on")
            # while still catching headlines like "AAPL Reports Q3".
            ticker_pat = _compile_ticker_pattern(clean_sym)

            for w in raw_list:
                head = w.get("headline", "")
                if not ticker_pat.search(head): continue
                item_date = w.get("date")
                if not item_date: continue  # legacy item; skip until it ages out
                try:
                    item_date_obj = datetime.strptime(item_date, "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    continue
                t_str = w.get("time", "")
                items.append({
                    "date": item_date, "time": t_str,
                    "age": self.get_time_ago(item_date_obj, t_str),
                    "headline": head, "url": w.get("url", ""),
                    "source": w.get("source", "Wire"),
                    "is_today": (item_date == today_iso)
                })
        except (KeyError, AttributeError):
            pass
        return items

    _EARNINGS_FORMATS = ("%b %d", "%b %d %Y", "%b %d, %Y", "%B %d",
                         "%B %d %Y", "%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d")

    def parse_earnings_date(self, earnings_str):
        """Best-effort parse of Finviz's Earnings cell. Common forms:
            "Mar 5"            "Mar 5 AMC"        "Mar 5/6"
            "Mar 5 BMO"        "Mar 5 - Mar 7"    "Mar 5, 2026"
            "3/5/2026"
        Returns the parsed date (year-resolved if absent) or None.
        Year resolution: walk forward/back up to ~9 months to land on
        the closest plausible occurrence."""
        if not earnings_str:
            return None
        # Strip trailing time-of-day codes (AMC = After Market Close,
        # BMO = Before Market Open, AH = After Hours).
        clean = re.sub(r'\s*(AMC|BMO|AH)\s*$', '', earnings_str, flags=re.IGNORECASE).strip()
        if not clean:
            return None

        dt = self._try_parse_date(clean)
        if dt is None:
            # Multi-day forms: trim to the first date.
            #   "Mar 5/6"      -> "Mar 5"  (slash-DD only)
            #   "Mar 5 - Mar 7" -> "Mar 5"
            #   "Mar 5,"       -> "Mar 5"
            stripped = re.sub(r'/\d{1,2}\s*$', '', clean).strip()
            stripped = re.split(r'\s*[-–—]\s*', stripped, maxsplit=1)[0].strip()
            stripped = stripped.rstrip(",").strip()
            if stripped and stripped != clean:
                dt = self._try_parse_date(stripped)
        if dt is None:
            return None

        now = datetime.now()
        # strptime defaults missing year to 1900 — resolve to closest
        # plausible occurrence relative to today.
        if dt.year < 1990:
            try:
                candidate = dt.replace(year=now.year)
            except ValueError:  # Feb 29 in non-leap year, etc.
                return None
            diff_days = (now.date() - candidate.date()).days
            if diff_days > 270:
                try:
                    candidate = candidate.replace(year=now.year + 1)
                except ValueError:
                    return None
            elif diff_days < -270:
                try:
                    candidate = candidate.replace(year=now.year - 1)
                except ValueError:
                    return None
            return candidate.date()
        return dt.date()

    @classmethod
    def _try_parse_date(cls, text):
        for fmt in cls._EARNINGS_FORMATS:
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    def earnings_proximity(self, earnings_str, past_days=9, future_days=9):
        """Return 'past' if earnings happened within the last `past_days`
        days (inclusive of today), 'future' if earnings are within the
        next `future_days` days (exclusive of today), else None."""
        edate = self.parse_earnings_date(earnings_str)
        if not edate: return None
        diff = (edate - datetime.now().date()).days  # positive = future, negative = past
        if diff < 0 and abs(diff) < past_days:
            return "past"
        if diff == 0:  # today
            return "past"
        if 0 < diff <= future_days:
            return "future"
        return None

    def _sec_throttle(self):
        # SEC fair-access throttle (10 req/s cap).
        with self._sec_lock:
            now_t = time.time()
            wait = MIN_SEC_INTERVAL - (now_t - self.last_sec_time)
            if wait > 0:
                time.sleep(wait)
            self.last_sec_time = time.time()

    @staticmethod
    def _max_filing_date(dates):
        """Find the most recent valid date in a SEC ``filingDate`` array.
        SEC convention is descending-by-date but the code no longer relies
        on that — it scans all entries and returns the max (R5)."""
        best = None
        for d in dates:
            try:
                dt = datetime.strptime(d, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if best is None or dt > best:
                best = dt
        return best

    def _fetch_submissions(self, cik_padded):
        """Fetch + cache the submissions JSON for ``cik_padded``. Sets
        ``self.sec_status`` to OK/ERR. Returns the parsed dict or None.

        Caching means scrape_sec_data and list_earnings_filings can
        both run against the same cached blob without refetching."""
        with self._submissions_cache_lock:
            cached = self._submissions_cache.get(cik_padded)
            if cached is not None:
                self._submissions_cache.move_to_end(cik_padded)
        if cached is not None:
            self.sec_status = "OK"
            return cached if cached else None
        url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        self._sec_throttle()
        try:
            r = self.session.get(url, headers=HEADERS, timeout=10, stream=True)
            if r.status_code != 200:
                self.sec_status = "ERR"
                # Cache empty dict so a 404/403 doesn't retry per
                # chart-open. Will retry next session.
                with self._submissions_cache_lock:
                    self._submissions_cache[cik_padded] = {}
                    self._evict_lru(self._submissions_cache,
                                    self._SUBMISSIONS_CACHE_MAX)
                return None
            # stream=True + _read_capped bounds the submissions blob before
            # json parse (this then lands in the cache, so capping it also
            # bounds resident cache memory).
            raw = _read_capped(r, _HTTP_MAX_BYTES_SEC_JSON)
            self.sec_status = "OK"
            data = json.loads(raw)
            with self._submissions_cache_lock:
                self._submissions_cache[cik_padded] = data
                self._evict_lru(self._submissions_cache,
                                self._SUBMISSIONS_CACHE_MAX)
            return data
        except (requests.RequestException, OSError, json.JSONDecodeError,
                KeyError, ValueError):
            self.sec_status = "ERR"
            return None

    def list_earnings_filings(self, resolved_cik):
        """Return ALL 10-K/10-Q filings from the cached submissions
        recent-block, sorted most-recent-first by file_date. Each
        entry: ``{"form", "accession", "file_date" (date), "report_date" (date|None)}``.

        Future-filed entries and amendments (10-K/A, 10-Q/A) are
        excluded — same strict filter as scrape_sec_data."""
        if not resolved_cik or not str(resolved_cik).isdigit():
            return []
        cik_padded = str(resolved_cik).zfill(10)
        data = self._fetch_submissions(cik_padded)
        if not data:
            return []
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        report_dates = recent.get("reportDate", [])
        today = datetime.now().date()
        out = []
        for i, form in enumerate(forms):
            form_u = (form or "").upper()
            if form_u not in ("10-K", "10-Q"):
                continue
            if i >= len(dates):
                continue
            try:
                f_date = datetime.strptime(dates[i], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if f_date > today:
                continue
            acc = accessions[i] if i < len(accessions) else ""
            r_date = None
            if i < len(report_dates) and report_dates[i]:
                try:
                    r_date = datetime.strptime(
                        report_dates[i], "%Y-%m-%d",
                    ).date()
                except (ValueError, TypeError):
                    r_date = None
            out.append({
                "form": form_u,
                "accession": acc,
                "file_date": f_date,
                "report_date": r_date,
            })
        out.sort(key=lambda r: r["file_date"], reverse=True)
        return out

    def scrape_sec_data(self, symbol, resolved_cik):
        """Single primary call to data.sec.gov for shelf (S-3) and
        recent-filing status. If no S-3 in the recent block AND the
        filer overflowed the recent cap, follow the pagination links
        in ``filings.files`` until either an in-window S-3 is found or
        we run out of pages (R4).

        Returns ``(has_s3, recent_status, recent_earnings)``.

        ``recent_earnings`` is the most-recent PAST 10-K/10-Q filing
        (file_date <= today, form is exactly ``10-K`` or ``10-Q`` — 8-Ks
        and amendments excluded so an earnings-announcement 8-K can't
        masquerade as the actual report). Shape:
            {"form", "accession", "file_date" (date), "report_date" (date|None)}
        or None when the CIK has no qualifying recent filing."""
        self.sec_status = None
        has_s3 = False
        recent_status = 2  # 0=<24h, 1=<48h, 2=>48h
        recent_earnings = None
        # Require a numeric CIK; bail out quietly otherwise (no spurious ERR).
        if not resolved_cik or not str(resolved_cik).isdigit():
            return has_s3, recent_status, recent_earnings
        cik_padded = str(resolved_cik).zfill(10)
        try:
            data = self._fetch_submissions(cik_padded)
            if not data:
                return has_s3, recent_status, recent_earnings
            recent = data.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])
            accessions = recent.get("accessionNumber", [])
            report_dates = recent.get("reportDate", [])
            today = datetime.now().date()
            cutoff_3y = today - timedelta(days=1095)

            # Most-recent filing date — scan all dates rather than
            # trusting recent[0] to be the max.
            top = self._max_filing_date(dates)
            if top is not None:
                diff = (today - top).days
                if diff <= 0: recent_status = 0
                elif diff <= 1: recent_status = 1

            # Check ``recent`` for S-3 within 3 years.
            has_s3 = self._scan_for_s3(forms, dates, cutoff_3y)

            # Most-recent past 10-K or 10-Q. Strict form match — 8-Ks
            # (even Item 2.02 earnings-release ones) and amendments
            # (10-K/A, 10-Q/A) are excluded so we never mistake an
            # announcement for the actual report. Scans all entries
            # rather than trusting array order.
            best_re_date = None
            for i, form in enumerate(forms):
                form_u = form.upper()
                if form_u not in ("10-K", "10-Q"):
                    continue
                if i >= len(dates):
                    continue
                try:
                    f_date = datetime.strptime(dates[i], "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    continue
                if f_date > today:
                    continue  # safeguard: future-filed entries (rare data glitch)
                if best_re_date is not None and f_date <= best_re_date:
                    continue
                acc = accessions[i] if i < len(accessions) else ""
                r_date = None
                if i < len(report_dates) and report_dates[i]:
                    try:
                        r_date = datetime.strptime(
                            report_dates[i], "%Y-%m-%d",
                        ).date()
                    except (ValueError, TypeError):
                        r_date = None
                best_re_date = f_date
                recent_earnings = {
                    "form": form_u,
                    "accession": acc,
                    "file_date": f_date,
                    "report_date": r_date,
                }

            # If we didn't find one, walk the older paginated files.
            # ``filings.files`` is a list of {"name": "CIK<x>-submissions-001.json", ...}
            # entries pointing to additional history. Most filers have 0
            # of these; a handful (high-volume issuers) have 1–3.
            if not has_s3:
                older_files = data.get("filings", {}).get("files", []) or []
                for f_meta in older_files:
                    fname = f_meta.get("name")
                    if not fname:
                        continue
                    # Validate the filename shape before interpolating it
                    # into the URL (defense-in-depth, mirroring the EDGAR
                    # adsh/primary-doc gates): a tampered/MITM'd value
                    # can't redirect the fetch to an unintended path.
                    if not (isinstance(fname, str)
                            and _SEC_SUBMISSIONS_FILE_RE.match(fname)):
                        continue
                    # Bail early if the file's date range is entirely
                    # outside our 3y window — older pages list a
                    # ``filingTo`` upper bound.
                    try:
                        page_to = datetime.strptime(
                            f_meta.get("filingTo", ""), "%Y-%m-%d",
                        ).date()
                        if page_to < cutoff_3y:
                            break
                    except (ValueError, TypeError):
                        pass
                    page_url = f"https://data.sec.gov/submissions/{fname}"
                    self._sec_throttle()
                    try:
                        rp = self.session.get(page_url, headers=HEADERS,
                                              timeout=10, stream=True)
                        if rp.status_code != 200:
                            continue
                        page_data = json.loads(
                            _read_capped(rp, _HTTP_MAX_BYTES_SEC_JSON))
                    except (requests.RequestException, OSError,
                            json.JSONDecodeError, ValueError):
                        continue
                    page_forms = page_data.get("form", [])
                    page_dates = page_data.get("filingDate", [])
                    if self._scan_for_s3(page_forms, page_dates, cutoff_3y):
                        has_s3 = True
                        break
        except (requests.RequestException, OSError, json.JSONDecodeError, KeyError):
            self.sec_status = "ERR"
        return has_s3, recent_status, recent_earnings

    @staticmethod
    def _scan_for_s3(forms, dates, cutoff_date):
        """Return True if any form in ``forms`` starts with 'S-3' and
        its corresponding date is on/after ``cutoff_date``."""
        for i, form in enumerate(forms):
            if not form.upper().startswith("S-3"):
                continue
            if i >= len(dates):
                continue
            try:
                f_date = datetime.strptime(dates[i], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if f_date >= cutoff_date:
                return True
        return False

    def fetch_finviz_earnings(self, symbol):
        """Fetch + parse the finviz ty=ea earnings page for ``symbol``,
        returning canonical per-quarter rows (with computed YoY), oldest
        -> newest. Cached per session per symbol and routed through the
        same rate limiter as ``scrape_finviz``. Returns ``[]`` on any
        failure / no coverage (cached too, so a miss isn't re-fetched).

        Network-bound: call OFF the Tk thread (chart-open path or a
        daemon backfill), never inside refresh_meta_label."""
        sym = (symbol or "").upper().strip()
        if not sym:
            return []
        with self._ea_cache_lock:
            if sym in self._ea_cache:
                self._ea_cache.move_to_end(sym)
                return self._ea_cache[sym]
        rows = []
        # Only cache a NEGATIVE ([] ) when the fetch genuinely completed
        # and the page is a real finviz quote page that simply has no
        # earnings coverage (ETF/fund/new). A transient block (non-200,
        # 429/403/captcha-200, timeout, exception, oversize body) must
        # NOT be cached — else one rate-limit on the first fetch would
        # poison this symbol's chart + landing-row backfill for the whole
        # session. Mirrors finviz_client's FAIL_EMPTY vs FAIL_BLOCKED
        # classification (and scrape_finviz never blanks on non-200).
        cache_result = False
        try:
            with self._scrape_lock:
                interval = self.finviz_min_interval
                now = time.time()
                if now - self.last_scrape_time < interval:
                    time.sleep(interval - (now - self.last_scrape_time))
                self.last_scrape_time = time.time()
            url = f"https://finviz.com/quote.ashx?t={url_quote(sym, safe='')}&ty=ea"
            r = self.session.get(url, headers=BROWSER_HEADERS, timeout=10,
                                 stream=True)
            if r.status_code == 200:
                # stream=True + _read_capped enforces the size cap BEFORE
                # the whole body is buffered (the prior len() check ran
                # after r.text had already materialized it, so the cap was
                # cosmetic). Oversize raises ValueError -> caught below ->
                # not cached, i.e. treated as a transient block.
                raw = _read_capped(r, _HTTP_MAX_BYTES_SCRAPE_HTML)
                body = raw.decode("utf-8", "ignore")
                entries = _fv_ea_extract(body)
                if entries:
                    rows = _fv_ea_rows_with_yoy(entries, sym)
                    cache_result = True
                else:
                    # No earningsData. Cache the empty ONLY when the
                    # page looks like a real finviz quote page (a true
                    # coverage miss), not a bot-challenge / captcha 200.
                    looks_like_quote = ("snapshot-td" in body) or (f"t={sym}" in body)
                    if looks_like_quote and len(body) > 50_000:
                        cache_result = True
        except Exception as exc:
            # Log the type so a parser regression in _fv_ea_* (which would
            # otherwise look identical to a genuine no-coverage miss) is
            # diagnosable. Never log the body/symbol-bearing message.
            _log.debug("fetch_finviz_earnings failed for %s: %s",
                       sym, type(exc).__name__)
            rows = []
            cache_result = False
        if cache_result:
            with self._ea_cache_lock:
                self._ea_cache[sym] = rows
                self._evict_lru(self._ea_cache, self._EA_CACHE_MAX)
        return rows

    def scrape_finviz(self, symbol):
        self.finviz_status = None
        with self._scrape_lock:
            interval = self.finviz_min_interval
            now = time.time()
            if now - self.last_scrape_time < interval:
                time.sleep(interval - (now - self.last_scrape_time))
            self.last_scrape_time = time.time()
        
        url = f"https://finviz.com/quote.ashx?t={url_quote(symbol, safe='')}&p=d"
        meta = {"name": "", "catalyst": "", "float": "", "short": "", "sector": "", "country": "", "mcap": "", "rvol": "", "is_low": False, "earnings": "", "eps_surprise": "", "sales_surprise": ""}
        items = []
        try:
            r = self.session.get(url, headers=BROWSER_HEADERS, timeout=10,
                                 stream=True)
            if r.status_code != 200:
                # Don't run BeautifulSoup on a captcha/rate-limit page —
                # it would silently blank fields that were valid before.
                self.finviz_status = "ERR"
                return meta, items
            # stream=True + _read_capped bounds the scraped HTML before it
            # is buffered into BeautifulSoup. Oversize raises ValueError ->
            # ERR (handled like a non-200, fields left untouched).
            raw = _read_capped(r, _HTTP_MAX_BYTES_SCRAPE_HTML)
            self.finviz_status = "OK"

            soup = BeautifulSoup(raw, "html.parser")
            
            try:
                title_text = soup.find("title").get_text(strip=True)
                if "-" in title_text:
                    parts = title_text.split("-", 1)[1]
                    name_clean = parts.split("Stock")[0].strip()
                    meta["name"] = name_clean
            except (AttributeError, TypeError):
                pass

            try:
                catalyst_node = soup.find(string=re.compile(r"Today,\s+\d{1,2}:\d{2}\s*[AP]M"))
                if catalyst_node:
                    container = catalyst_node.parent
                    full_text = container.get_text(strip=True, separator=" ")
                    if len(full_text) < 25:
                        full_text = container.parent.get_text(strip=True, separator=" ")
                    meta["catalyst"] = full_text
            except (AttributeError, TypeError):
                pass

            for a in soup.find_all("a", href=True):
                if "f=sec_" in a["href"]: meta["sector"] = a.get_text(strip=True)
                elif "f=geo_" in a["href"]: meta["country"] = a.get_text(strip=True)
                if meta["sector"] and meta["country"]: break
            
            # Finviz redesigned the quote page: the snapshot grid is now
            # SIX separate <table class="snapshot-table2"> columns (each row a
            # [label, value] pair), not one wide table. Walk them all — find()
            # only saw the first column, silently dropping Earnings, EPS/Sales
            # Surpr., Shs Float, Short Float and Rel Volume.
            for snap in soup.find_all("table", class_="snapshot-table2"):
                for tr in snap.find_all("tr"):
                    tds = tr.find_all("td")
                    # Snapshot table is laid out as label/value/label/value
                    # pairs across columns — step by 2 so we don't visit
                    # each pair twice.
                    for i in range(0, len(tds)-1, 2):
                        txt = tds[i].get_text(strip=True).lower()
                        val = tds[i+1].get_text(strip=True)
                        if "shs float" in txt: meta["float"], meta["is_low"] = self.parse_float(val)
                        elif "short float" in txt: meta["short"] = val
                        elif "market cap" in txt: meta["mcap"] = val
                        elif "rel volume" in txt: meta["rvol"] = val
                        elif txt == "earnings": meta["earnings"] = val
                        elif "eps/sales" in txt and "surpr" in txt:
                            # Two ordered slots (EPS / Sales); either may be a
                            # bare "-" (N/A). See _parse_eps_sales_surpr_cell.
                            eps_s, sales_s = _parse_eps_sales_surpr_cell(tds[i+1])
                            if eps_s is not None:
                                meta["eps_surprise"] = eps_s
                            if sales_s is not None:
                                meta["sales_surprise"] = sales_s

            news_table = soup.find(id="news-table")
            if news_table:
                today_date = datetime.now().date()
                curr_date = today_date
                for tr in news_table.find_all("tr"):
                    tds = tr.find_all("td")
                    if len(tds) < 2: continue
                    ts_txt = tds[0].get_text(strip=True)
                    time_part = ""
                    if "Today" in ts_txt: curr_date = today_date
                    elif "Yesterday" in ts_txt: curr_date = today_date - timedelta(days=1)
                    elif "-" in ts_txt:
                        try: curr_date = datetime.strptime(ts_txt.split()[0], "%b-%d-%y").date()
                        except (ValueError, IndexError): pass
                    if ts_txt.endswith("M"): time_part = ts_txt.split()[-1]
                    link = tds[1].find("a")
                    headline = link.get_text(strip=True) if link else tds[1].get_text(strip=True)
                    items.append({
                        "date": curr_date.isoformat(), "time": time_part, "age": self.get_time_ago(curr_date, time_part),
                        "headline": headline, "url": link["href"] if link else "", "source": "Finviz", "is_today": (curr_date == today_date)
                    })
        except (requests.RequestException, OSError, AttributeError, ValueError):
            # ValueError covers an oversize body from _read_capped.
            self.finviz_status = "ERR"
        return meta, items

# ==============================================================================
# WINDOW WATCHERS — one per trading-platform modality.
# All watchers expose the same interface: .get_info() -> (symbol, name|None)
# ==============================================================================
class TSWindowWatcher:
    """TradeStation desktop. Finds the 'MARKET DEPTH' / 'MATRIX' child
    window under any top-level 'TradeStation' window and parses the
    symbol + company name out of its title via ' - ' delimiters."""

    def __init__(self):
        self.DEPTH_MARKERS = ["MARKET DEPTH", "MATRIX"]

    def _clean_symbol(self, token):
        s = re.sub(r"\(.*?\)", "", token)
        s = re.sub(r"\[.*?\]", "", s)
        s = re.sub(r"[^A-Z]", "", s)
        return s.strip()

    def _parse_depth_title(self, title):
        upper = title.upper().replace("–", "-").replace("—", "-")
        if not any(m in upper for m in self.DEPTH_MARKERS): return None, None
        parts = upper.split(" - ")
        found_sym = None
        found_name = None
        if len(parts) >= 2:
            raw_sym = parts[1].strip().split(" ")[0]
            found_sym = self._clean_symbol(raw_sym)
        if len(parts) >= 3:
            # Bound + strip control chars on the untrusted company-name
            # token from a foreign app's window title (unlike found_sym it
            # has no char-class gate). Display-only today, but capping it
            # at the source future-proofs against any later URL/path sink.
            found_name = re.sub(r"[\x00-\x1f\x7f]", "", parts[2].strip())[:64]
        if not found_sym:
            m = re.search(r"([A-Z]+(?:\([A-Z]+\))?)\s*\[", upper)
            if m: found_sym = self._clean_symbol(m.group(1))
        if found_sym and 1 <= len(found_sym) <= 5:
            return found_sym, found_name
        return None, None

    def get_info(self):
        found_symbol = None
        found_name = None
        def child_enum_handler(hwnd, ctx):
            nonlocal found_symbol, found_name
            if found_symbol: return 0
            title = win32gui.GetWindowText(hwnd)
            if any(m in title.upper() for m in self.DEPTH_MARKERS):
                sym, name = self._parse_depth_title(title)
                if sym:
                    found_symbol = sym; found_name = name
                    return 0
            return 1
        def top_enum_handler(hwnd, ctx):
            if found_symbol: return
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if "TradeStation" in title:
                    try: win32gui.EnumChildWindows(hwnd, child_enum_handler, None)
                    except OSError: pass
        try: win32gui.EnumWindows(top_enum_handler, None)
        except OSError: pass
        return found_symbol, found_name


class TitanWindowWatcher:
    """TradeStation TITAN X (Electron). Uses UI Automation to find
    Image elements named 'Chart {SYMBOL} {type}' (e.g. 'Chart SII
    candle') inside the TITAN X top-level window. Falls back to tab
    buttons like 'SII Daily'.

    Uses a TreeWalker for early exit on the first match instead of
    materializing every Image/Button via FindAll (E5). FindAll is kept
    as a deterministic fallback so behavior is a strict superset of
    the prior implementation."""

    _CHART_RE = re.compile(r"^Chart\s+([A-Z]{1,5})\s+\w+", re.IGNORECASE)
    _IMAGE_CONTROL_TYPE = 50006
    _BUTTON_CONTROL_TYPE = 50000
    # Hard caps on tree-walker traversal so a pathological TITAN layout
    # cannot hang the watcher even with no per-call timeout from the
    # UIA API itself.
    _MAX_NODES_PER_PASS = 4000
    _MAX_DEPTH = 40

    def __init__(self):
        self._uia = None
        self._root = None
        self._UIA = None
        self._image_walker = None
        self._button_walker = None
        if _HAS_COMTYPES:
            self._init_uia()

    def _init_uia(self):
        try:
            self._UIA = comtypes.client.GetModule('UIAutomationCore.dll')
            self._uia = comtypes.CoCreateInstance(
                self._UIA.CUIAutomation._reg_clsid_,
                interface=self._UIA.IUIAutomation,
                clsctx=comtypes.CLSCTX_INPROC_SERVER,
            )
            self._root = self._uia.GetRootElement()
            # Pre-build TreeWalkers once. They're expensive to construct
            # and don't change between calls.
            try:
                img_cond = self._uia.CreatePropertyCondition(
                    self._UIA.UIA_ControlTypePropertyId, self._IMAGE_CONTROL_TYPE,
                )
                self._image_walker = self._uia.CreateTreeWalker(img_cond)
            except Exception:
                self._image_walker = None
            try:
                btn_cond = self._uia.CreatePropertyCondition(
                    self._UIA.UIA_ControlTypePropertyId, self._BUTTON_CONTROL_TYPE,
                )
                self._button_walker = self._uia.CreateTreeWalker(btn_cond)
            except Exception:
                self._button_walker = None
        except Exception:
            self._uia = None

    def _walk_for_match(self, walker, root, predicate):
        """Iterative DFS using a TreeWalker, short-circuiting on the
        first node whose ``CurrentName``/``CurrentClassName`` makes
        ``predicate`` return a truthy result. Returns whatever the
        predicate returns, or None."""
        if walker is None or root is None:
            return None
        try:
            node = walker.GetFirstChildElement(root)
        except Exception:
            return None
        # Stack of (node, depth) — emulates recursion without blowing
        # the Python stack on deep Electron trees.
        stack = []
        if node is not None:
            stack.append((node, 0))
        seen = 0
        while stack and seen < self._MAX_NODES_PER_PASS:
            node, depth = stack.pop()
            seen += 1
            try:
                hit = predicate(node)
            except Exception:
                hit = None
            if hit:
                return hit
            if depth < self._MAX_DEPTH:
                try:
                    child = walker.GetFirstChildElement(node)
                except Exception:
                    child = None
                if child is not None:
                    stack.append((child, depth + 1))
            try:
                sib = walker.GetNextSiblingElement(node)
            except Exception:
                sib = None
            if sib is not None:
                stack.append((sib, depth))
        return None

    def get_info(self):
        if not self._uia:
            return None, None
        try:
            cond = self._uia.CreatePropertyCondition(
                self._UIA.UIA_NamePropertyId, "TradeStation TITAN X"
            )
            titan = self._root.FindFirst(self._UIA.TreeScope_Children, cond)
            if not titan:
                return None, None

            # 1. Tree-walker pass for chart Images — short-circuits on
            #    the first match (cheapest path on most layouts).
            def chart_pred(node):
                try:
                    name = node.CurrentName or ""
                except Exception:
                    return None
                m = self._CHART_RE.match(name)
                if m:
                    return (m.group(1).upper(), None)
                return None

            hit = self._walk_for_match(self._image_walker, titan, chart_pred)
            if hit is not None:
                return hit

            # 2. Tree-walker pass for tab Buttons.
            def button_pred(node):
                try:
                    name = node.CurrentName or ""
                    cls = node.CurrentClassName or ""
                except Exception:
                    return None
                if "items-center justify" in cls:
                    parts = name.split()
                    if parts and 1 <= len(parts[0]) <= 5 and parts[0].isalpha():
                        return (parts[0].upper(), None)
                return None

            hit = self._walk_for_match(self._button_walker, titan, button_pred)
            if hit is not None:
                return hit

            # 3. Fallback: original FindAll path. Kept verbatim so the
            #    new walker can never *regress* on a layout the old
            #    code handled — only ever match faster.
            try:
                img_cond = self._uia.CreatePropertyCondition(
                    self._UIA.UIA_ControlTypePropertyId, self._IMAGE_CONTROL_TYPE,
                )
                images = titan.FindAll(self._UIA.TreeScope_Descendants, img_cond)
                for i in range(images.Length):
                    elem = images.GetElement(i)
                    name = elem.CurrentName or ""
                    m = self._CHART_RE.match(name)
                    if m:
                        return m.group(1).upper(), None
            except Exception:
                pass
            try:
                btn_cond = self._uia.CreatePropertyCondition(
                    self._UIA.UIA_ControlTypePropertyId, self._BUTTON_CONTROL_TYPE,
                )
                buttons = titan.FindAll(self._UIA.TreeScope_Descendants, btn_cond)
                for i in range(buttons.Length):
                    elem = buttons.GetElement(i)
                    name = elem.CurrentName or ""
                    cls = elem.CurrentClassName or ""
                    if "items-center justify" in cls:
                        parts = name.split()
                        if parts and 1 <= len(parts[0]) <= 5 and parts[0].isalpha():
                            return parts[0].upper(), None
            except Exception:
                pass
        except Exception:
            pass
        return None, None


class TVWindowWatcher:
    """TradingView Desktop (Chrome/Electron). Title format:
      '{SYMBOL} ▲ {price} {change%} / {panel} {timeframe}'
    e.g. 'VYGR ▲ 5.00 +21.65% / Middle 1D'.
    Matches the first 1-5 letter token followed by a triangle or digit."""

    _TV_RE = re.compile(r"^([A-Z]{1,5})\s+[\u25B2\u25BC\d]")

    def __init__(self):
        pass

    def get_info(self):
        found_symbol = None
        def enum_handler(hwnd, ctx):
            nonlocal found_symbol
            if found_symbol:
                return
            if win32gui.IsWindowVisible(hwnd):
                try:
                    cls = win32gui.GetClassName(hwnd)
                except OSError:
                    return
                if cls != "Chrome_WidgetWin_1":
                    return
                title = win32gui.GetWindowText(hwnd)
                m = self._TV_RE.match(title)
                if m:
                    found_symbol = m.group(1)
        try:
            win32gui.EnumWindows(enum_handler, None)
        except OSError:
            pass
        return found_symbol, None


WATCH_MODES = ("TS", "TITAN", "TV")
WATCH_LABELS = {"TS": "TS", "TITAN": "TITAN", "TV": "TV"}


class WatchThread:
    """Runs the active window-watcher on a dedicated daemon thread so
    a slow UIA call in TITAN mode (or any other watcher stall) can
    never freeze the Tk main loop. Calls pythoncom.CoInitialize() on
    the worker thread so UIA COM objects live in a valid apartment —
    which is the root cause of the original exe freezing over time.

    The main thread never blocks; it just polls get_latest().

    Watchdog: ``last_call_started_at`` is updated immediately before
    each ``get_info()`` call and ``last_call_finished_at`` is updated
    immediately after. If the gap grows beyond ``STALL_THRESHOLD_SEC``,
    callers (the UI status loop) can detect a stall via
    ``is_stalled()``. We can't *interrupt* a blocked UIA call from
    Python — the call lives in C — but we can surface the stall so the
    user knows the symbol indicator is stale instead of confidently
    showing wrong info.
    """

    POLL_SEC = 0.5
    STALL_THRESHOLD_SEC = 8.0

    def __init__(self, initial_mode="TS"):
        self._mode = initial_mode
        self._lock = threading.Lock()
        self._latest = (None, None)
        self._running = True
        # Event-based shutdown so stop() wakes the poll loop immediately
        # instead of waiting out a full POLL_SEC sleep (lets on_close
        # join the worker before CoUninitialize / teardown).
        self._stop_evt = threading.Event()
        self._call_start = 0.0
        self._call_end = 0.0
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="MS-WatchThread",
        )
        self._thread.start()

    def set_mode(self, mode):
        with self._lock:
            self._mode = mode
            self._latest = (None, None)

    def get_latest(self):
        with self._lock:
            return self._latest

    def is_stalled(self):
        """True if a get_info() call has been running longer than
        STALL_THRESHOLD_SEC without returning."""
        with self._lock:
            start = self._call_start
            end = self._call_end
        if start == 0.0 or end >= start:
            return False
        return (time.time() - start) > self.STALL_THRESHOLD_SEC

    def stop(self):
        self._running = False
        self._stop_evt.set()

    def join(self, timeout=None):
        """Wait for the worker to exit (bounded — daemon=True guarantees
        process exit regardless, so a wedged UIA call can't hang close)."""
        try:
            self._thread.join(timeout)
        except Exception:
            pass

    def _run(self):
        # pythoncom ships with pywin32 (already a hard dep). UIA is
        # STA-friendly; default CoInitialize is STA.
        try:
            import pythoncom
            pythoncom.CoInitialize()
            _co_inited = True
        except Exception:
            _co_inited = False
        try:
            watchers = {
                "TS": TSWindowWatcher(),
                "TITAN": TitanWindowWatcher(),
                "TV": TVWindowWatcher(),
            }
            while self._running:
                try:
                    with self._lock:
                        mode = self._mode
                        self._call_start = time.time()
                    w = watchers.get(mode)
                    result = w.get_info() if w is not None else (None, None)
                    with self._lock:
                        self._call_end = time.time()
                        # Only publish the result if the mode hasn't
                        # been switched under us during the call.
                        if mode == self._mode:
                            self._latest = result
                except Exception:
                    with self._lock:
                        self._call_end = time.time()
                    # A misbehaving watcher must never kill the thread.
                    pass
                # Interruptible sleep: stop() wakes this immediately.
                self._stop_evt.wait(self.POLL_SEC)
        finally:
            if _co_inited:
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

# ==============================================================================
# HISTORICAL LOOKUP — Polygon news + EDGAR full-text search
# ==============================================================================

# Default form filter for the EDGAR full-text catalyst search. Covers
# the catalyst events most likely to drive a single-day price move
# (8-K/6-K materials, dilution-related S-1/S-3/424B*, insider Form 4,
# activist 13D/G, late-filing notices). User can tune this in Settings.
DEFAULT_HISTORICAL_FORMS = ("10-K,10-Q,8-K,6-K,424B2,424B3,424B5,"
                             "S-1,S-3,4,SC 13D,SC 13G,NT 10-K,NT 10-Q")
# NOTE: amendment forms (10-K/A, 8-K/A, etc.) are deliberately omitted
# from the default. EDGAR's full-text-search forms parameter does not
# accept slashes — passing '10-K/A' returns 0 hits regardless of URL
# encoding. Users who specifically want amendments can add them via
# Settings, but the search will silently miss them.

# Polygon free tier is 5 req/min — historical is on-demand and only
# fires single calls per Lookup click, so no internal throttle needed.
POLYGON_NEWS_URL = "https://api.polygon.io/v2/reference/news"
EDGAR_FULLTEXT_URL = "https://efts.sec.gov/LATEST/search-index"

# Per-endpoint HTTP response-size caps. EDGAR companyfacts blobs run
# 1–10 MB legitimately; we cap at 50 MB so a hostile / misbehaving
# origin can't allocate gigabytes of memory in `r.text` / `r.json()`.
# Scraped HTML pages (Finviz, EDGAR full-text-search hits, RSS-linked
# articles) cap tighter at 5 MB — none of them should exceed this in
# normal use.
_HTTP_MAX_BYTES_COMPANYFACTS = 50 * 1024 * 1024
_HTTP_MAX_BYTES_SCRAPE_HTML = 5 * 1024 * 1024
# SEC submissions JSON (the manifest + per-filer history pages) is a few
# MB for the heaviest filers; cap generously at 50 MB. Polygon/EDGAR
# full-text JSON and RSS bodies are small — they reuse the 5 MB cap.
_HTTP_MAX_BYTES_SEC_JSON = 50 * 1024 * 1024


# Module-level logger. WARNING + above go to stderr by default;
# downstream callers can attach file handlers if needed. Switching off
# bare ``print`` for errors means URL-shaped exception strings stop
# leaking into the console (which can land in screenshots, etc.).
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
_log = logging.getLogger("MorningScanner")


def _build_accn_index(facts_dict):
    """Pre-index every us-gaap fact by accession number so the per-
    filing lookups in HistoricalLookup are O(facts_for_filing) instead
    of O(all_facts). Returns ``{accession: [fact_dict, ...]}``. Stored
    on the facts dict under the ``_accn_index`` key by the cache layer
    so HistoricalLookup._facts_for_filing can fast-path through it
    without changing its signature."""
    index = {}
    ns = (facts_dict or {}).get("facts", {}).get("us-gaap", {})
    for tag_name, tag_data in ns.items():
        for unit_name, fact_list in (tag_data.get("units") or {}).items():
            for f in fact_list:
                accn = f.get("accn")
                if not accn:
                    continue
                index.setdefault(accn, []).append({
                    "tag": tag_name, "unit": unit_name, **f,
                })
    return index


def _scrub_polygon_key(text, key):
    """Belt-and-suspenders: redact ``key`` from any string we're about
    to surface to the user (notice rows, log lines, exception reprs).
    Polygon key is now in the Authorization header rather than the URL
    so this should never fire — kept as defense in depth."""
    if not text or not key:
        return text
    return text.replace(key, "***REDACTED***")


@lru_cache(maxsize=512)
def _compile_ticker_pattern(symbol_upper):
    """Build (once per ticker) the headline-search regex that
    ``DataFetcher.get_wires`` runs against every cached wire. Wires
    cache is up to 500 items and we previously rebuilt this regex on
    every symbol change — now it's a one-shot per (ticker) lookup."""
    esc = re.escape(symbol_upper)
    return re.compile(
        rf"(?:(?:\$|[A-Z]{{2,6}}:\s*|\([A-Z]{{2,6}}:\s*|\(){esc}\b)"
        rf"|(?:(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9]))"
    )


def _read_capped(response, max_bytes):
    """Drain a streamed Response in 64 KB chunks, raising ``ValueError``
    if the body exceeds ``max_bytes``. Caller is expected to have
    issued the request with ``stream=True``."""
    buf = bytearray()
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise ValueError(f"response exceeded {max_bytes} bytes")
    return bytes(buf)

# Defense-in-depth validators for EDGAR-sourced URL components. Both
# fields come from EDGAR's full-text response JSON and we don't want a
# malformed (or hostile-MITM-injected) value to path-traverse out of
# /Archives/edgar/data/. SEC accession numbers are always
# ``NNNNNNNNNN-YY-NNNNNN``; primary docs are always filename-shaped.
_EDGAR_ADSH_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
# Must start with an alphanumeric: this rejects the dot-only segments
# ``.``/``..``/``...`` that the old ``^[A-Za-z0-9._-]+$`` admitted and
# that normalize to a parent directory on the server (intra-host path
# traversal). Real EDGAR primary docs are always filename-shaped and
# begin with a letter or digit, so nothing valid is lost.
_EDGAR_PRIMARY_DOC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Additional-history submissions pages are always named
# ``CIK<10 digits>-submissions-<3 digits>.json``. Validating ``name``
# before interpolating it into the data.sec.gov URL closes the one
# URL-construction site that previously trusted the field verbatim.
_SEC_SUBMISSIONS_FILE_RE = re.compile(r"^CIK\d{10}-submissions-\d{3}\.json$")


class HistoricalLookup:
    """Fetches catalyst evidence for a (ticker, date) pair from
    Polygon (Massive) news and EDGAR full-text search. Stateless;
    each method does one HTTP round-trip and returns a list of
    unified-shape result dicts.

    Result dict shape:
        {
            "source": "polygon" | "edgar",
            "when":   <ISO datetime str, ET>,   # for sorting / display
            "type":   <publisher name | form code>,
            "title":  <headline | filing description>,
            "url":    <article URL | filing primary doc URL>,
            "extra":  {  # source-specific metadata
                # polygon:
                "sentiment": "positive"|"neutral"|"negative"|None,
                "tickers":   [str, ...],
                # edgar:
                "items":      [str, ...],   # 8-K item codes
                "accession":  str,
                "form":       str,
                "file_date":  "YYYY-MM-DD",
            }
        }
    """

    @staticmethod
    def polygon_news(ticker, target_date, api_key,
                     days_before=2, days_after=2,
                     max_tickers=5, timeout=15):
        """Query Polygon ticker-news for a window around target_date.
        Returns (results, error_msg). On success error_msg is "".
        Articles tagged with more than ``max_tickers`` tickers are
        filtered out (set max_tickers=0 to disable that filter)."""
        if not api_key:
            return [], "no_key"
        try:
            d = datetime.strptime(target_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return [], "bad_date"
        start = (d - timedelta(days=days_before)).strftime("%Y-%m-%d")
        end = (d + timedelta(days=days_after)).strftime("%Y-%m-%d")
        params = {
            "ticker": ticker.upper().strip(),
            "published_utc.gte": start,
            "published_utc.lte": end,
            "limit": 1000,
            "order": "asc",
        }
        # Pass the API key in the Authorization header rather than as a
        # ``?apiKey=…`` query param. The query-param form leaks the key
        # into ``requests`` exception messages, which the caller surfaces
        # as a notice row + into any printed traceback. Polygon's REST
        # API supports Bearer auth as an equivalent alternative.
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            r = requests.get(
                POLYGON_NEWS_URL, params=params, headers=headers,
                timeout=timeout, stream=True,
            )
        except (requests.RequestException, OSError) as exc:
            # ``repr(exc)`` should no longer contain the key, but scrub
            # defensively in case Polygon ever echoes it elsewhere.
            return [], f"net: {_scrub_polygon_key(str(exc), api_key)}"
        if r.status_code == 429:
            return [], "rate_limit"
        if r.status_code == 401:
            return [], "bad_key"
        if r.status_code != 200:
            return [], f"http_{r.status_code}"
        try:
            # stream=True + _read_capped bounds the response before parse.
            payload = json.loads(_read_capped(r, _HTTP_MAX_BYTES_SCRAPE_HTML))
        except (ValueError, requests.RequestException, OSError):
            return [], "bad_json"
        raw = payload.get("results", []) or []
        results = []
        tk_upper = ticker.upper().strip()
        for art in raw:
            tickers_arr = art.get("tickers") or []
            if max_tickers and len(tickers_arr) > max_tickers:
                continue
            sentiment = None
            for ins in (art.get("insights") or []):
                if (ins.get("ticker") or "").upper() == tk_upper:
                    sentiment = ins.get("sentiment")
                    break
            results.append({
                "source": "polygon",
                "when": art.get("published_utc", "") or "",
                "type": (art.get("publisher") or {}).get("name", "") or "",
                "title": art.get("title", "") or "",
                "url": art.get("article_url", "") or "",
                "extra": {
                    "sentiment": sentiment,
                    "tickers": list(tickers_arr),
                },
            })
        return results, ""

    @staticmethod
    def edgar_fulltext(cik_padded, target_date, forms, ua,
                       days_before=2, days_after=5, timeout=15):
        """Query EDGAR full-text search for filings in an asymmetric
        window around target_date (wider on the forward side because
        the 8-K filing deadline is 4 business days after the event).
        Returns (results, error_msg). ua = User-Agent string with a
        contact (SEC requirement)."""
        if not cik_padded:
            return [], "no_cik"
        try:
            d = datetime.strptime(target_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return [], "bad_date"
        start = (d - timedelta(days=days_before)).strftime("%Y-%m-%d")
        end = (d + timedelta(days=days_after)).strftime("%Y-%m-%d")
        params = {
            "dateRange": "custom",
            "startdt": start,
            "enddt": end,
            "forms": forms,
            "ciks": str(cik_padded).zfill(10),
            "size": 100,
        }
        headers = {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}
        try:
            r = requests.get(EDGAR_FULLTEXT_URL, params=params,
                              headers=headers, timeout=timeout, stream=True)
        except (requests.RequestException, OSError) as exc:
            return [], f"net: {exc}"
        if r.status_code == 429:
            return [], "rate_limit"
        if r.status_code == 403:
            return [], "ua_block"
        if r.status_code != 200:
            return [], f"http_{r.status_code}"
        try:
            # stream=True + _read_capped bounds the response before parse.
            payload = json.loads(_read_capped(r, _HTTP_MAX_BYTES_SCRAPE_HTML))
        except (ValueError, requests.RequestException, OSError):
            return [], "bad_json"
        hits = ((payload.get("hits") or {}).get("hits") or [])
        results = []
        # Strip leading zeros for the Archives URL — EDGAR's submissions
        # endpoint wants padded CIKs, but /Archives/edgar/data/ wants the
        # unpadded integer. Both are required (per the markdown).
        cik_int = str(int(cik_padded))
        for h in hits:
            src = h.get("_source") or {}
            adsh = src.get("adsh") or ""
            _id = h.get("_id") or ""
            primary_doc = ""
            if ":" in _id:
                primary_doc = _id.split(":", 1)[1]
            # Defense-in-depth: only let well-shaped strings into the
            # constructed Archives URL. Hostile / malformed inputs are
            # downgraded to the safe ``browse-edgar`` redirect, which
            # carries only the CIK and can't path-traverse.
            adsh_ok = bool(_EDGAR_ADSH_RE.match(adsh)) if adsh else False
            primary_doc_ok = (
                bool(_EDGAR_PRIMARY_DOC_RE.match(primary_doc))
                if primary_doc else False
            )
            url = ""
            if adsh_ok and primary_doc_ok:
                no_dash = adsh.replace("-", "")
                url = (f"https://www.sec.gov/Archives/edgar/data/"
                       f"{cik_int}/{no_dash}/{primary_doc}")
            elif adsh_ok:
                url = (f"https://www.sec.gov/cgi-bin/browse-edgar?"
                       f"action=getcompany&CIK={cik_int}"
                       f"&action=getcompany")
            form_str = src.get("form") or src.get("root_form") or ""
            file_date = src.get("file_date") or ""
            items = src.get("items") or []
            descr = src.get("file_description") or form_str
            title = descr or form_str
            if items:
                title = f"{title}  [{','.join(items)}]"
            when_str = file_date
            results.append({
                "source": "edgar",
                "when": when_str,
                "type": form_str,
                "title": title,
                "url": url,
                "extra": {
                    "items": list(items),
                    "accession": adsh,
                    "form": form_str,
                    "file_date": file_date,
                },
            })
        return results, ""

    # ------------------------------------------------------------------
    # Enrichment helpers — fetch supplementary detail for EDGAR rows
    # so the user can scan results without clicking through.
    # ------------------------------------------------------------------

    # XBRL companyfacts API. Returns every reported us-gaap fact for a
    # company across all periods. We use this to compute YoY %s on
    # 10-K/10-Q filings.
    EDGAR_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

    # Tag-fallback chains — companies tag the same concept with
    # different us-gaap tag names. Walk each chain and use the first
    # tag that has data for the requested period.
    YOY_REVENUE_TAGS = (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    )
    YOY_EPS_TAGS = (
        "EarningsPerShareDiluted",
        "EarningsPerShareBasic",
        "IncomeLossFromContinuingOperationsPerDilutedShare",
    )

    @staticmethod
    def companyfacts(cik_padded, ua, timeout=20):
        """Fetch the full companyfacts JSON blob for a CIK. Returns
        (data, error_str). Caller is responsible for caching the
        result for the session — these blobs are 1–10 MB and nearly
        static.

        Response is streamed in 64 KB chunks with a 50 MB hard cap so
        a malformed / hostile origin can't balloon memory."""
        if not cik_padded:
            return None, "no_cik"
        url = HistoricalLookup.EDGAR_COMPANYFACTS_URL.format(cik=cik_padded)
        headers = {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}
        try:
            r = requests.get(url, headers=headers, timeout=timeout, stream=True)
        except (requests.RequestException, OSError) as exc:
            return None, f"net: {exc}"
        try:
            if r.status_code == 404:
                return None, "not_found"  # foreign filer / no XBRL
            if r.status_code == 429:
                return None, "rate_limit"
            if r.status_code != 200:
                return None, f"http_{r.status_code}"
            try:
                raw = _read_capped(r, _HTTP_MAX_BYTES_COMPANYFACTS)
            except (ValueError, requests.RequestException, OSError) as exc:
                return None, f"size_or_io: {exc}"
            try:
                return json.loads(raw), ""
            except ValueError:
                return None, "bad_json"
        finally:
            try: r.close()
            except Exception: pass

    @staticmethod
    def _facts_for_filing(facts_dict, accession):
        """Return every us-gaap fact whose ``accn`` matches ``accession``.
        Each fact carries `tag`, `unit`, plus all the original fields
        (start/end/fp/fy/val).

        Fast path: when the facts dict was inserted via the ScannerApp
        XBRL cache, ``_accn_index`` is attached — a precomputed
        ``{accession: [facts]}`` map. Look-up is O(1) on the dict.

        Slow path (raw companyfacts dict, no index attached): linear
        walk of every tag × unit × fact in the namespace."""
        if not facts_dict or not accession:
            return []
        idx = facts_dict.get("_accn_index")
        if idx is not None:
            return list(idx.get(accession, []))
        out = []
        ns = facts_dict.get("facts", {}).get("us-gaap", {})
        for tag_name, tag_data in ns.items():
            for unit_name, fact_list in (tag_data.get("units") or {}).items():
                for f in fact_list:
                    if f.get("accn") == accession:
                        out.append({
                            "tag": tag_name, "unit": unit_name, **f,
                        })
        return out

    @staticmethod
    def _find_fact_by_period(facts_dict, tag_chain,
                              start_iso, end_iso, tolerance_days=0):
        """Find a us-gaap period fact value matching (start, end). When
        ``tolerance_days > 0`` the lookup accepts facts whose start
        AND end are each within ``tolerance_days`` of the targets —
        used for prior-year comparison on companies with 52/53-week
        fiscal calendars (NVDA, AAPL) where the year-prior period
        end can shift by a few days.

        Walks ``tag_chain`` left-to-right; first tag with a hit wins."""
        ns = (facts_dict or {}).get("facts", {}).get("us-gaap", {})
        if not ns:
            return None
        try:
            start_dt = datetime.strptime(start_iso, "%Y-%m-%d").date()
            end_dt = datetime.strptime(end_iso, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None
        for tag in tag_chain:
            entry = ns.get(tag)
            if not entry:
                continue
            for unit_bucket in (entry.get("units") or {}).values():
                best = None
                best_diff = None
                for f in unit_bucket:
                    if f.get("form") not in ("10-K", "10-Q"):
                        continue
                    fs = f.get("start"); fe = f.get("end")
                    if not fs or not fe:
                        continue
                    if tolerance_days == 0:
                        if fs != start_iso or fe != end_iso:
                            continue
                        diff = 0
                    else:
                        try:
                            fs_dt = datetime.strptime(fs, "%Y-%m-%d").date()
                            fe_dt = datetime.strptime(fe, "%Y-%m-%d").date()
                        except (ValueError, TypeError):
                            continue
                        ds = abs((fs_dt - start_dt).days)
                        de = abs((fe_dt - end_dt).days)
                        if ds > tolerance_days or de > tolerance_days:
                            continue
                        diff = ds + de
                    val = f.get("val")
                    if val is None:
                        continue
                    if best is None or diff < best_diff or (
                        diff == best_diff
                        and f.get("filed", "") > best.get("filed", "")
                    ):
                        best = f
                        best_diff = diff
                if best is not None:
                    return float(best["val"])
        return None

    @staticmethod
    def filing_period(facts_dict, accession):
        """Return (start_iso, end_iso) for the period this filing
        primarily reports, by walking companyfacts entries whose
        ``accn`` matches ``accession``. Income-statement period-flow
        facts are preferred (they have both `start` and `end`); the
        entry with the latest `end` wins. Returns (None, None) if
        the filing isn't covered by us-gaap facts (foreign issuers,
        small-caps without XBRL)."""
        filing_facts = HistoricalLookup._facts_for_filing(facts_dict, accession)
        if not filing_facts:
            return None, None
        period_facts = [f for f in filing_facts
                         if f.get("start") and f.get("end")]
        if not period_facts:
            return None, None
        period_facts.sort(key=lambda f: f.get("end", ""))
        rep = period_facts[-1]
        return rep.get("start"), rep.get("end")

    @staticmethod
    def extract_yoy(facts_dict, accession):
        """Compute YoY % growth for Revenue + EPS at the period
        reported by ``accession``. Returns
        ``{"rev_yoy": float|None, "eps_yoy": float|None}``.

        Discovers the filing's exact reporting period by inspecting
        the (start, end) of facts whose ``accn`` matches the filing.
        EDGAR's `fp`/`fy` fields describe the FILING's fiscal period,
        NOT the period of each fact — a 10-K typically reports the
        same revenue concept at three different (start,end) tuples
        (current FY, prior FY, two-years-prior FY) all tagged with
        ``fp=FY fy=<filing's FY>``. Matching by (start,end) is the
        only robust way to identify the current-period number.

        Prior-year is the same period shifted back ~1 year. A small
        tolerance handles 52/53-week fiscal calendars (NVDA, AAPL)
        where the year-shifted period can drift by a few days."""
        out = {"rev_yoy": None, "eps_yoy": None}
        if not facts_dict or not accession:
            return out
        filing_facts = HistoricalLookup._facts_for_filing(facts_dict, accession)
        if not filing_facts:
            return out
        # Drop instant facts (balance-sheet items have `end` but no
        # `start`); we want period-flow facts (income statement).
        period_facts = [f for f in filing_facts
                         if f.get("start") and f.get("end")]
        if not period_facts:
            return out
        # Pick the entry with the most recent `end` — that's the
        # current period the filing primarily reports. The same
        # filing also carries comparatives (prior FY/Q) at earlier
        # `end` dates; we ignore those as the current-period anchor.
        period_facts.sort(key=lambda f: f.get("end", ""))
        rep = period_facts[-1]
        cur_start = rep.get("start")
        cur_end = rep.get("end")
        if not cur_start or not cur_end:
            return out
        try:
            cur_start_dt = datetime.strptime(cur_start, "%Y-%m-%d").date()
            cur_end_dt = datetime.strptime(cur_end, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return out
        try:
            prior_start_dt = cur_start_dt.replace(year=cur_start_dt.year - 1)
            prior_end_dt = cur_end_dt.replace(year=cur_end_dt.year - 1)
        except ValueError:
            return out
        prior_start = prior_start_dt.isoformat()
        prior_end = prior_end_dt.isoformat()
        for tag_chain, key, floor in (
            (HistoricalLookup.YOY_REVENUE_TAGS, "rev_yoy", _YOY_MIN_BASE_REV_RAW),
            (HistoricalLookup.YOY_EPS_TAGS, "eps_yoy", _YOY_MIN_BASE_EPS),
        ):
            cur = HistoricalLookup._find_fact_by_period(
                facts_dict, tag_chain, cur_start, cur_end,
                tolerance_days=0,
            )
            # Prior-year lookup tolerates ±7-day drift (52/53-week
            # fiscal calendars shift the year-prior period by a few
            # days vs. an exact 365-day rollback).
            prior = HistoricalLookup._find_fact_by_period(
                facts_dict, tag_chain, prior_start, prior_end,
                tolerance_days=7,
            )
            # Small-base floor (== the parquet + ty=ea policy): skip a YoY
            # whose prior-year denominator is below the meaningful threshold.
            if (cur is not None and prior is not None
                    and abs(prior) >= floor):
                out[key] = (cur - prior) / abs(prior) * 100.0
        return out

    # 1-liner extraction for 8-K / 6-K / NT 10-K / NT 10-Q rows.
    # 8-Ks routinely open with "On {date}, {Company} ('the Company')
    # {verb} ...". We strip those layers off one at a time so the
    # snippet starts at the verb (issued / announced / entered / etc.).
    # All three regexes use re.UNICODE because the boilerplate often
    # contains curly quotes ("apple"). Apple's iXBRL filings live in
    # <span> tags rather than <p> — handled below by widening the tag
    # walk to span / div / td.
    _ONELINER_DATE_RE = re.compile(
        r'^\s*On\s+[A-Z][a-z]+\.?\s+\d{1,2}\s*,?\s*\d{4}\s*,?\s*',
        re.UNICODE,
    )
    # Word-boundary anchor (`\b`) on the suffix word stops `Corp` from
    # matching the leading 4 chars of `Corporation` (alternation is
    # left-to-right and first-match-wins, so `Corp` would otherwise
    # win over `Corporation`). Longer-form-first ordering as a belt-
    # and-suspenders defense.
    _ONELINER_COMPANY_RE = re.compile(
        r"^\s*[A-Z][\w\.\-&',\s]{0,80}?"
        r"(?:Corporation|Incorporated|Holdings|Company|Group|Inc|Corp|Co\.?|Ltd|LLC|plc|N\.A\.)\b\.?"
        r"(?:\s*,\s*(?:or\s+the\s+(?:Company|Issuer)|or\s+the\s+\w+))?"
        r"\s*,?\s*",
        re.UNICODE,
    )
    # Match a parenthetical that names the company short-hand:
    #   ("the Company") | ('the Company') | (“Apple”) | ('Acme') | (we) | (collectively, "we") etc.
    # Just match anything non-closing-paren up to the closing paren —
    # cleaner than enumerating every quote/separator possibility, and
    # an 8-K opening paren is always a company short-name reference.
    _ONELINER_PAREN_RE = re.compile(
        r"^\([^)]{1,120}\)\s*,?\s*",
        re.UNICODE,
    )
    # Noise filter for cover-page boilerplate. Matches strings that
    # appear before the substantive narrative in standard EDGAR filings
    # (the SEC header, the cover-page checkboxes, the address block,
    # the registrant identifier block, etc.). Case-insensitive so it
    # catches both "Pursuant to" and "PURSUANT TO".
    _ONELINER_NOISE_RE = re.compile(
        r'^(?:the\s+)?(?:'
        r'Pursuant\s+to|Check\s+the|Indicate\s+by|If\s+an\s+emerging|'
        r'SECURITIES\s+AND\s+EXCHANGE|UNITED\s+STATES|FORM\s+\d|'
        r'Date\s+of\s+[Rr]eport|Date\s+of\s+earliest|'
        r'Securities\s+Exchange\s+Act|Securities\s+Act\s+of|'
        r'Report\s+of\s+Foreign|Foreign\s+Private\s+Issuer|'
        r'\(Exact\s+[Nn]ame|\(State\s+or\s+other|\(I\.R\.S\.|\(Commission|'
        r'\(Address|\(Zip\s+Code|\(Registrant|\(Former|\(Date\s+of|'
        r'Address\s+of\s+Principal|'
        r'Securities\s+registered|Name\s+of\s+each\s+exchange|'
        r'Title\s+of\s+each\s+class|Trading\s+Symbol|'
        r'Item\s+\d+\.\d{1,2}\b|'        # Item header echoes
        r'Results\s+of\s+Operations\s+and\s+Financial\s+Condition\s*$|'  # Section title
        r'Financial\s+Statements\s+and\s+Exhibits\s*$|'
        r'Registrant\W'
        r')',
        re.IGNORECASE | re.UNICODE,
    )

    # Common 8-K / 10-Q narrative verbs. The candidate picker prefers
    # candidates containing one of these so a section-title span like
    # "Results of Operations and Financial Condition" loses to the
    # substantive "On Jan 29, 2025, Microsoft issued a press release..."
    # span that follows it.
    _ONELINER_NARRATIVE_VERBS = (
        "issued", "announced", "entered into", "completed", "agreed",
        "declared", "received", "terminated", "acquired", "released",
        "furnished", "appointed", "reported", "disclosed", "filed",
        "approved", "executed", "amended", "consummated", "elected",
    )

    @staticmethod
    def _strip_oneliner_boilerplate(text):
        """Peel the "On {date}, Company X ('the Company') " prefix
        off ``text``. Anchored on the date match — the company-name
        and paren strips only run when the date strip succeeded, so
        legitimate prose like 'The Company reported …' is not
        touched (the company-suffix regex would otherwise match
        'The Company' as a corp-suffix word and eat valid text)."""
        after_date, n_date = HistoricalLookup._ONELINER_DATE_RE.subn(
            "", text, count=1,
        )
        if n_date == 0:
            return text
        text = after_date
        text = HistoricalLookup._ONELINER_COMPANY_RE.sub("", text, count=1)
        text = HistoricalLookup._ONELINER_PAREN_RE.sub("", text, count=1)
        return text

    @staticmethod
    def extract_oneliner(url, ua, timeout=15, max_chars=120, full_chars=800):
        """Fetch the primary doc HTML and extract a short one-line
        summary suitable for inline display. Returns
        (snippet, full, error_str) — ``snippet`` is up to
        ``max_chars`` (with trailing ellipsis when clipped),
        ``full`` is up to ``full_chars`` of the same text plus the
        next-best candidate appended for tooltip context. On any
        failure both texts are empty.

        Strategy: walk a broad set of element types (`<p>` for normal
        HTML; `<span>`/`<div>`/`<td>` for iXBRL filings), filter out
        XBRL metadata + boilerplate rule references + Item-header
        echoes, prefer candidates that open with 'On {date}, Company...',
        peel off the date+company+paren boilerplate, and clip on a
        word boundary at ``max_chars``."""
        if not url:
            return "", "", "no_url"
        # Self-defend: this public static method fetches whatever URL it
        # is handed. The only current caller passes regex-validated
        # sec.gov URLs, but an internal https+sec.gov guard means a future
        # caller handing it an arbitrary (e.g. Polygon article) URL can't
        # turn it into a blind SSRF fetch primitive.
        try:
            _pu = urlparse(url)
            if _pu.scheme != "https" or not (
                    _pu.hostname or "").lower().endswith(".sec.gov"):
                return "", "", "bad_host"
        except (ValueError, AttributeError):
            return "", "", "bad_host"
        headers = {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}
        try:
            r = requests.get(url, headers=headers, timeout=timeout, stream=True)
        except (requests.RequestException, OSError) as exc:
            return "", "", f"net: {exc}"
        try:
            if r.status_code == 429:
                return "", "", "rate_limit"
            if r.status_code != 200:
                return "", "", f"http_{r.status_code}"
            try:
                raw = _read_capped(r, _HTTP_MAX_BYTES_SCRAPE_HTML)
            except (ValueError, requests.RequestException, OSError) as exc:
                return "", "", f"size_or_io: {exc}"
        finally:
            try: r.close()
            except Exception: pass
        try:
            soup = BeautifulSoup(raw, "html.parser")
        except Exception:
            return "", "", "parse"

        def _is_substantive_candidate(txt):
            """Return True if ``txt`` looks like substantive narrative
            (not cover-page boilerplate / metadata / header echo)."""
            if not txt or len(txt) < 25:
                return False
            # XBRL metadata noise: lots of digits/colons/underscores.
            noise_ratio = sum(1 for ch in txt
                                if ch.isdigit() or ch in ":_/") / max(len(txt), 1)
            if noise_ratio > 0.3:
                return False
            # ALL-CAPS headers ("SECURITIES AND EXCHANGE COMMISSION",
            # "FORM 8-K") — substantive prose always has lowercase.
            if not any(c.islower() for c in txt):
                return False
            # Cover-page boilerplate (registrant block, address, item
            # header echoes, "Pursuant to Section 13..." checkboxes).
            if HistoricalLookup._ONELINER_NOISE_RE.match(txt):
                return False
            # Fully parenthetical labels: "(Registrant's telephone
            # number)", "(I.R.S. Employer Identification No.)".
            if txt.startswith('(') and txt.endswith(')'):
                return False
            # Rule/regulation references that appear as cover-page
            # checkbox labels.
            if ("Rule" in txt
                    and ("Securities Act" in txt or "Exchange Act" in txt)
                    and len(txt) < 250):
                return False
            return True

        # Two-phase walk:
        #   Phase 1 — walk in document order until we cross the first
        #             "Item N.NN" header, then collect substantive
        #             elements that follow. 8-K / 10-K / 10-Q always
        #             have Item or Part headers; the substantive
        #             narrative always sits after them, so this is the
        #             most reliable selector when available.
        #   Phase 2 — fallback for filings without Item/Part headers
        #             (6-K, NT 10-K, etc.): just walk all elements and
        #             keep substantive ones.
        # `find_all([...])` returns elements in document tree order.
        candidates = []
        in_item_section = False
        for el in soup.find_all(["p", "span", "div", "td"]):
            txt = " ".join((el.get_text(" ", strip=True) or "").split())
            if not txt:
                continue
            # Detect the Item / Part header that gates substantive
            # content — match even short header strings (we don't
            # require length 25 here).
            if re.match(r'^(?:Item|PART|Part)\s+\w+\.?\s*\d*\.?\d*\b', txt):
                in_item_section = True
                continue
            if not in_item_section:
                continue
            if not _is_substantive_candidate(txt):
                continue
            # Cap each candidate before the downstream per-char noise scan
            # + boilerplate regex passes so a single huge iXBRL block can't
            # spike CPU (a oneliner only needs the leading sentence).
            candidates.append(txt[:2000])
            if len(candidates) >= 20:
                break

        # Fallback for filings with no Item header (e.g., 6-K).
        if not candidates:
            for el in soup.find_all(["p", "span", "div", "td"]):
                txt = " ".join((el.get_text(" ", strip=True) or "").split())
                if not _is_substantive_candidate(txt):
                    continue
                candidates.append(txt[:2000])
                if len(candidates) >= 20:
                    break

        if not candidates:
            return "", "", "no_text"

        # Pre-strip every candidate once so we can pick from the
        # stripped versions and also build the longer ``full`` blurb
        # for the hover tooltip.
        stripped = []
        for c in candidates:
            s = HistoricalLookup._strip_oneliner_boilerplate(c).strip(" ,;:.")
            stripped.append(s)

        # Two-pass picker:
        #   Pass 1 — prefer the FIRST candidate whose stripped content
        #            (a) is substantive (>= 30 chars) AND (b) contains
        #            a known narrative verb. This loses to short section
        #            titles like "Results of Operations and Financial
        #            Condition" which lack verbs, and to legal-disclaimer
        #            paragraphs whose verbs are not in the list.
        #   Pass 2 — fall back to the first substantive (>= 30 chars)
        #            candidate ignoring verbs. Handles filings whose
        #            narrative uses a verb not in our list.
        #   Pass 3 — last resort: longest raw candidate.
        pick_idx = -1
        for i, s in enumerate(stripped):
            if len(s) < 30:
                continue
            s_lower = s.lower()
            if any(v in s_lower for v in HistoricalLookup._ONELINER_NARRATIVE_VERBS):
                pick_idx = i
                break
        if pick_idx < 0:
            for i, s in enumerate(stripped):
                if len(s) >= 30:
                    pick_idx = i
                    break
        if pick_idx < 0:
            picked = max(candidates, key=len).strip(" ,;:.")
            full = picked
            text = picked
        else:
            picked = stripped[pick_idx]
            text = picked
            # Build the longer hover blurb. Start with the picked
            # paragraph, then append the next 1-2 substantive
            # paragraphs (separated by paragraph breaks) up to
            # ``full_chars``. Use raw candidate text (not boilerplate-
            # stripped) for follow-on paragraphs since they're new
            # paragraphs, not continuations of the picked one.
            full_parts = [picked]
            running = len(picked)
            for j in range(pick_idx + 1, len(candidates)):
                if running >= full_chars:
                    break
                extra = candidates[j].strip(" ,;:.")
                if not extra or len(extra) < 25:
                    continue
                full_parts.append(extra)
                running += len(extra) + 2
                if len(full_parts) >= 3:
                    break
            full = "\n\n".join(full_parts)
            if len(full) > full_chars:
                cut = full.rfind(" ", 0, full_chars)
                if cut < int(full_chars * 0.6):
                    cut = full_chars
                full = full[:cut].rstrip(" ,;:.") + "…"

        if len(text) > max_chars:
            cut = text.rfind(" ", 0, max_chars)
            if cut < int(max_chars * 0.6):
                cut = max_chars
            text = text[:cut].rstrip(" ,;:.") + "…"
        return text, full, ""


# ==============================================================================
# UI
# ==============================================================================
class ScannerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        # The SEC placeholder-contact warning is emitted later, after
        # load_settings + _set_sec_contact have had a chance to apply a
        # Settings-menu / env-var contact (see the commit block below).
        self.fetcher = DataFetcher()
        self.watch_mode = tk.StringVar(value="TS")
        # Lazily built at the end of __init__ once settings have been
        # loaded so the worker thread starts in the user's saved mode.
        self.watch_thread = None
        self.current_symbol = None
        self.current_window_name = None
        self.current_cik = None
        self.current_meta = {}
        # Most-recent past 10-K/10-Q for the current ticker, captured
        # by scrape_sec_data on each symbol change. Used by the earnings
        # resolver (EDGAR-tier date fallback) and the async XBRL YoY
        # backfill so we never have to re-walk the submissions API.
        self.current_recent_earnings = None
        # Background async YoY backfill — a per-symbol generation
        # counter so a stale fetch can't overwrite a newer ticker's
        # labels. Bumped on every change_symbol.
        self._earnings_yoy_gen = 0
        self.hot_words_new = []
        self.hot_words_old = []
        self.base_font_size = 10
        self.theme_mode = "dark"
        self._pending_show_earnings = False
        # Custom ETF-map JSON path. Empty string means "use the default
        # alongside-the-exe location". Loaded from settings later.
        self._pending_etf_map_custom_path: str = ""
        self.etf_map_custom_path: str = ""
        self.etf_map: EtfMap | None = None
        self.colors = THEMES[self.theme_mode]
        
        self.title("Morning Scanner")
        self.configure(bg=self.colors["BG"])
        self.attributes("-topmost", True)
        self.minsize(600, 420)
        self.load_settings()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.style = ttk.Style()
        self.style.theme_use("clam")
        
        self.hdr = tk.Frame(self, bg=self.colors["BG"])
        self.hdr.pack(fill="x", padx=10, pady=5)
        
        self.header_top = tk.Frame(self.hdr, bg=self.colors["BG"])
        self.header_top.pack(side="top", fill="x", anchor="w")

        self.lbl_symbol = tk.Label(self.header_top, text="—", bg=self.colors["BG"], fg=self.colors["FG"])
        self.lbl_symbol.pack(side="left", anchor="sw")

        # MCap: always shown, large font (the slot Float used to hold),
        # optionally painted with the 5-tier stepped gradient.
        self.lbl_mcap = tk.Label(self.header_top, text="", bg=self.colors["BG"], fg=self.colors["FG"])
        self.lbl_mcap.pack(side="left", padx=(15, 5), anchor="sw", pady=(0, 4))

        # Float: toggleable (control-row checkbox), default (smaller) font,
        # optional low/high coloration.
        self.lbl_float = tk.Label(self.header_top, text="", bg=self.colors["BG"], fg=self.colors["TXT_OK"])
        self.lbl_float.pack(side="left", padx=(0, 5), anchor="sw", pady=(0, 4))

        self.lbl_sec_recent = tk.Label(self.header_top, text="SEC: —", bg=self.colors["BG"], fg=self.colors["FG"])
        self.lbl_sec_recent.pack(side="left", padx=5, anchor="sw", pady=(0, 4))
        self.lbl_sec_recent.configure(cursor="hand2")
        self.lbl_sec_recent.bind("<Button-1>", self.open_sec_recent)

        self.lbl_shelf = tk.Label(self.header_top, text="Shelf: —", bg=self.colors["BG"], fg=self.colors["FG"])
        self.lbl_shelf.pack(side="left", padx=5, anchor="sw", pady=(0, 4))
        self.lbl_shelf.configure(cursor="hand2")
        self.lbl_shelf.bind("<Button-1>", self.open_sec_shelf_link)

        # ETF indicator — gray "ETF: NO" when symbol isn't a known
        # underlying and isn't itself a leveraged ETF; white "ETF: YES"
        # when the symbol has tracking ETFs in the map (click → popup
        # dropdown); blue "ETF: {UND} {mult}x" when the symbol IS one
        # of the leveraged ETFs in the map (click → Finviz for the
        # underlying).
        self.lbl_etf = tk.Label(
            self.header_top, text="ETF: —",
            bg=self.colors["BG"], fg=self.colors["CREDIT"],
        )
        self.lbl_etf.pack(side="left", padx=5, anchor="sw", pady=(0, 4))
        self.lbl_etf.bind("<Button-1>", self._on_etf_label_click)
        # Hover tooltip when the active symbol IS a multi-holding ETF —
        # lists its current constituents (alphabetical).
        self.lbl_etf.bind("<Enter>", self._on_etf_label_enter)
        self.lbl_etf.bind("<Leave>", self._on_etf_label_leave)

        # Second ETF indicator ("column 2"): when the active symbol is a
        # STOCK, shows how many multi-holding ETFs (levered or not) hold
        # it as a top-N constituent. Click → popup listing them. Hidden
        # (text "—") when the symbol has no such holders or is itself an ETF.
        self.lbl_etf_hold = tk.Label(
            self.header_top, text="",
            bg=self.colors["BG"], fg=self.colors["CREDIT"],
        )
        self.lbl_etf_hold.pack(side="left", padx=5, anchor="sw", pady=(0, 4))
        self.lbl_etf_hold.bind("<Button-1>", self._on_etf_hold_label_click)

        self.lbl_meta = tk.Label(self.header_top, text="", bg=self.colors["BG"], fg=self.colors["FG"])
        self.lbl_meta.pack(side="left", padx=5, anchor="sw", pady=(0, 4))

        self.earnings_row = tk.Frame(self.hdr, bg=self.colors["BG"])
        self.earnings_row.pack(side="top", fill="x", anchor="w")
        self.lbl_earnings = tk.Label(
            self.earnings_row, text="", bg=self.colors["BG"], fg=self.colors["FG"],
            cursor="hand2",
        )
        self.lbl_earnings.pack(side="left", padx=(0, 2), anchor="w")
        # Double-click the "Earn: …" label to open a chart of the
        # ticker's quarterly EPS / Revenue history.
        self.lbl_earnings.bind("<Double-Button-1>", self.open_earnings_chart)
        self.lbl_eps_surp = tk.Label(self.earnings_row, text="", bg=self.colors["BG"], fg=self.colors["FG"], cursor="hand2")
        self.lbl_eps_surp.pack(side="left", padx=2, anchor="w")
        self.lbl_eps_surp.bind("<Double-Button-1>", self.open_earnings_chart)
        self.lbl_sales_surp = tk.Label(self.earnings_row, text="", bg=self.colors["BG"], fg=self.colors["FG"], cursor="hand2")
        self.lbl_sales_surp.pack(side="left", padx=2, anchor="w")
        self.lbl_sales_surp.bind("<Double-Button-1>", self.open_earnings_chart)
        # YoY %s — merged parquet primary, EDGAR XBRL last-resort
        # backfill; never Finviz (its "Q/Q" growth fields are unreliable
        # for near-breakeven EPS). Colored blue (positive) / pink
        # (negative) always, to distinguish growth %s from surprise %s.
        # Palette matches the historical-lookup YoY tags.
        self.lbl_eps_yoy = tk.Label(self.earnings_row, text="", bg=self.colors["BG"], fg=self.colors["FG"], cursor="hand2")
        self.lbl_eps_yoy.pack(side="left", padx=2, anchor="w")
        self.lbl_eps_yoy.bind("<Double-Button-1>", self.open_earnings_chart)
        self.lbl_rev_yoy = tk.Label(self.earnings_row, text="", bg=self.colors["BG"], fg=self.colors["FG"], cursor="hand2")
        self.lbl_rev_yoy.pack(side="left", padx=2, anchor="w")
        self.lbl_rev_yoy.bind("<Double-Button-1>", self.open_earnings_chart)

        # [CHANGE] Added cursor="hand2" and bound Button-1 for clickability
        self.lbl_name = tk.Label(self.hdr, text="", bg=self.colors["BG"], fg=self.colors["VIOLET"], anchor="w", justify="left", cursor="hand2", wraplength=500)
        self.lbl_name.pack(side="top", anchor="w", fill="x")
        self.lbl_name.bind("<Button-1>", self.open_finviz_link)

        # Highlight bars + "Last Refreshed" share one row. Highlights
        # sit on the left (New = leftmost, green; Old = right of New,
        # red); Last Refreshed is right-aligned on the far right.
        self.refresh_info = tk.Frame(self, bg=self.colors["BG"])
        self.refresh_info.pack(fill="x", padx=10, pady=(0, 0))

        self.lbl_highlight_new = tk.Label(
            self.refresh_info, text="New:", bg=self.colors["BG"], fg="#888888"
        )
        self.lbl_highlight_new.pack(side="left", padx=(0, 3))
        self.entry_hot_new = tk.Entry(self.refresh_info, width=18)
        self.entry_hot_new.pack(side="left")
        self.entry_hot_new.bind("<Return>", self.apply_hot_words)
        self.btn_apply_new = tk.Button(
            self.refresh_info, text="Apply",
            command=self.apply_hot_words, borderwidth=0, padx=8,
        )
        self.btn_apply_new.pack(side="left", padx=(3, 15))

        self.lbl_highlight_old = tk.Label(
            self.refresh_info, text="Old:", bg=self.colors["BG"], fg="#888888"
        )
        self.lbl_highlight_old.pack(side="left", padx=(0, 3))
        self.entry_hot_old = tk.Entry(self.refresh_info, width=18)
        self.entry_hot_old.pack(side="left")
        self.entry_hot_old.bind("<Return>", self.apply_hot_words)
        self.btn_apply_old = tk.Button(
            self.refresh_info, text="Apply",
            command=self.apply_hot_words, borderwidth=0, padx=8,
        )
        self.btn_apply_old.pack(side="left", padx=(3, 15))

        # Mode selector — three mutually-exclusive radios pick which
        # platform the window-watcher scrapes the active symbol from.
        self.mode_radios = {}
        for code in WATCH_MODES:
            rb = tk.Radiobutton(
                self.refresh_info, text=WATCH_LABELS[code],
                variable=self.watch_mode, value=code,
                command=self._on_mode_change,
            )
            rb.pack(side="left", padx=2)
            self.mode_radios[code] = rb

        self.lbl_last_refresh = tk.Label(
            self.refresh_info, text="Last Refreshed: —",
            bg=self.colors["BG"], fg="#888888",
        )
        self.lbl_last_refresh.pack(side="right", anchor="e")

        # Search row — collapsible. Lives directly under the highlight
        # row. The toggle button on the right of the highlight row
        # (▾/▸) flips visibility.
        self.search_visible = tk.BooleanVar(value=False)
        self.btn_search_toggle = tk.Button(
            self.refresh_info, text="🔍 ▸",
            command=self._toggle_search_row,
            borderwidth=0, padx=8,
        )
        self.btn_search_toggle.pack(side="right", padx=(6, 6))

        self.search_row = tk.Frame(self, bg=self.colors["BG"])
        # Don't pack yet — user toggles it.

        self.lbl_search_kw = tk.Label(
            self.search_row, text="Search:",
            bg=self.colors["BG"], fg="#888888",
        )
        self.lbl_search_kw.pack(side="left", padx=(0, 3))
        self.entry_search_kw = tk.Entry(self.search_row, width=20)
        self.entry_search_kw.pack(side="left")
        self.entry_search_kw.bind("<Return>", self.apply_search)

        self.lbl_search_date = tk.Label(
            self.search_row, text="Date:",
            bg=self.colors["BG"], fg="#888888",
        )
        self.lbl_search_date.pack(side="left", padx=(15, 3))
        self.entry_search_date = tk.Entry(self.search_row, width=22)
        self.entry_search_date.pack(side="left")
        self.entry_search_date.bind("<Return>", self.apply_search)

        self.btn_search_apply = tk.Button(
            self.search_row, text="Apply",
            command=self.apply_search, borderwidth=0, padx=8,
        )
        self.btn_search_apply.pack(side="left", padx=(3, 6))

        self.btn_search_clear = tk.Button(
            self.search_row, text="Clear",
            command=self.clear_search, borderwidth=0, padx=8,
        )
        self.btn_search_clear.pack(side="left", padx=(0, 6))

        self.lbl_search_help = tk.Label(
            self.search_row,
            text='quotes = whole word; date: 2026-04-29 or 4/29/26, today, yesterday, 7d, A..B',
            bg=self.colors["BG"], fg="#888888",
        )
        self.lbl_search_help.pack(side="left", padx=(8, 0))

        self.lbl_search_err = tk.Label(
            self.search_row, text="",
            bg=self.colors["BG"], fg=self.colors["TXT_BAD"],
        )
        # Packed only when there's an error.

        self.search_keywords = []  # parsed terms (same shape as hot words)
        self.search_date_pred = None  # callable (date_iso_str) -> bool, or None

        # Historical lookup row — third (bottom) row of the search panel.
        # Always visible whenever the search panel is open. Triggers an
        # on-demand Polygon news + EDGAR full-text search around the
        # entered date for the active chart symbol; results take over
        # the wires Treeview until the user hits Exit, refresh, or the
        # active symbol changes.
        self.historical_active = False
        self.historical_date = None  # datetime.date or None
        self.historical_results: list = []
        self._historical_busy = False
        self._historical_gen = 0
        # Saved column widths for restoring wires layout when exiting
        # historical mode. Populated on enter, consumed on exit.
        self._wires_col_widths = None
        # Per-session enrichment caches. companyfacts blobs are 1–10 MB
        # and nearly static; 1-liners come from primary docs that
        # never change after filing. Both keyed by stable IDs.
        # OrderedDict + lock + LRU eviction so a long session can't
        # balloon resident memory (audit-flagged: 50 CIKs × ~5 MB =
        # 250 MB without the cap). Concurrent enrichment runs are
        # also collapsed onto a single fetch via the ``_*_get_or_fetch``
        # helpers below.
        self._xbrl_facts_cache: "OrderedDict[str, dict]" = OrderedDict()
        self._xbrl_facts_cache_lock = threading.RLock()
        self._oneliner_cache: "OrderedDict[str, tuple]" = OrderedDict()
        self._oneliner_cache_lock = threading.RLock()

        # Sibling of search_row so it stacks vertically below it; the
        # toggle handler packs/unpacks both together.
        self.historical_row = tk.Frame(self, bg=self.colors["BG"])

        self.lbl_historical = tk.Label(
            self.historical_row, text="Historical:",
            bg=self.colors["BG"], fg="#888888",
        )
        self.lbl_historical.pack(side="left", padx=(0, 3))
        self.entry_historical_date = tk.Entry(self.historical_row, width=22)
        self.entry_historical_date.pack(side="left")
        # Enter triggers Lookup the same as the button. The
        # `_historical_busy` gate in run_historical_lookup prevents
        # double-fire if the user mashes Enter while a fetch is in
        # flight.
        self.entry_historical_date.bind(
            "<Return>", lambda e: self.run_historical_lookup(),
        )

        self.btn_historical_lookup = tk.Button(
            self.historical_row, text="Lookup",
            command=self.run_historical_lookup, borderwidth=0, padx=8,
        )
        self.btn_historical_lookup.pack(side="left", padx=(6, 3))
        self.btn_historical_exit = tk.Button(
            self.historical_row, text="Exit",
            command=self.exit_historical_mode, borderwidth=0, padx=8,
        )
        self.btn_historical_exit.pack(side="left", padx=(0, 6))

        self.lbl_historical_help = tk.Label(
            self.historical_row,
            text="single date only — pulls Polygon news + EDGAR filings around the date",
            bg=self.colors["BG"], fg="#888888",
        )
        self.lbl_historical_help.pack(side="left", padx=(8, 0))

        self.lbl_historical_err = tk.Label(
            self.historical_row, text="",
            bg=self.colors["BG"], fg=self.colors["TXT_BAD"],
        )
        # Packed only when there's an error (same pattern as search err).

        self.ctrl = tk.Frame(self, bg=self.colors["BG"])
        self.ctrl.pack(fill="x", padx=10, pady=(0, 5))
        
        self.var_48 = tk.BooleanVar(value=False)
        self.chk_48 = tk.Checkbutton(self.ctrl, text="48h", variable=self.var_48, command=self.refresh_ui)
        self.chk_48.pack(side="left")

        self.var_all = tk.BooleanVar(value=False)
        self.chk_all = tk.Checkbutton(self.ctrl, text="All", variable=self.var_all, command=self.refresh_ui)
        self.chk_all.pack(side="left", padx=(5,0))
        
        self.var_float = tk.BooleanVar(value=False)
        self.chk_float = tk.Checkbutton(self.ctrl, text="Float", variable=self.var_float, command=self._render_float_label)
        self.chk_float.pack(side="left", padx=(5,0))
        
        self.var_rvol = tk.BooleanVar(value=False)
        self.chk_rvol = tk.Checkbutton(self.ctrl, text="Rel Vol", variable=self.var_rvol, command=self.refresh_meta_label)
        self.chk_rvol.pack(side="left")

        self.var_earnings = tk.BooleanVar(value=False)
        self.chk_earnings = tk.Checkbutton(self.ctrl, text="Earnings", variable=self.var_earnings, command=self.refresh_meta_label)
        self.chk_earnings.pack(side="left", padx=(5, 0))

        self.btn_plus = tk.Button(self.ctrl, text="+", command=lambda: self.adjust_font(1), borderwidth=0, padx=8)
        self.btn_plus.pack(side="left", padx=(5,1))
        self.btn_minus = tk.Button(self.ctrl, text="-", command=lambda: self.adjust_font(-1), borderwidth=0, padx=8)
        self.btn_minus.pack(side="left", padx=1)
        
        self.btn_theme = tk.Button(self.ctrl, text="☀/☾", command=self.toggle_theme, borderwidth=0, padx=8)
        self.btn_theme.pack(side="left", padx=(5,1))

        self.btn_settings = tk.Button(
            self.ctrl, text="⚙", command=self.open_settings_dialog,
            borderwidth=0, padx=8,
        )
        self.btn_settings.pack(side="left", padx=1)

        # Manual refresh button — the timestamp it writes to lives on
        # its own row above (see ``self.refresh_info``).
        self.btn_refresh = tk.Button(
            self.ctrl, text="↻", command=self.manual_refresh,
            borderwidth=0, padx=8,
        )
        self.btn_refresh.pack(side="left", padx=(5, 1))

        self.stat_frame = tk.Frame(self.ctrl, bg=self.colors["BG"])
        self.stat_frame.pack(side="right", padx=10)
        self.indicators = {}
        self.status_widgets = {}
        # Tracks the last (theme, color) we applied to each indicator
        # so status_loop can skip the .config() call when nothing
        # changed. ~430k redundant widget updates/day saved.
        self._last_indicator_colors: dict = {}
        for i, code in enumerate(["PR", "GB", "YH", "FV", "SEC"]):
            f = tk.Frame(self.stat_frame, bg=self.colors["BG"])
            f.pack(side="left", padx=2)
            lbl = tk.Label(f, text=code, bg=self.colors["BG"], fg="#888888")
            lbl.pack(side="top")
            box = tk.Label(f, text="", width=2, height=1, bg=self.colors["STATUS_WAIT"])
            box.pack(side="bottom", pady=(1,0))
            self.indicators[code] = box
            self.status_widgets[code] = (lbl, f, box)

        self.tree = ttk.Treeview(self, columns=("date", "age", "headline"), show="headings", selectmode="browse")
        self.tree.heading("date", text="Date")
        self.tree.heading("age", text="Age")
        self.tree.heading("headline", text="Headline")
        # Date + Age are fixed: stretch=False keeps whatever width the
        # user has dragged them to. Only Headline expands/contracts
        # with the window.
        self.tree.column("date", width=80, anchor="center", stretch=False)
        self.tree.column("age", width=70, anchor="center", stretch=False)
        self.tree.column("headline", width=500, anchor="w", stretch=True)
        # Restore any widths the user dragged in a prior session.
        for _col, _w in self._pending_col_widths.items():
            try:
                self.tree.column(_col, width=int(_w))
            except tk.TclError:
                pass
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True, padx=10, pady=5)
        self.tree.bind("<Double-1>", self.on_double_click)
        # Hover tooltip for historical-mode rows that carry an
        # auto-generated 1-liner. Shows the longer ``oneliner_full``
        # blurb so users can scan beyond the truncated row title.
        self._tooltip_win = None
        self._tooltip_after = None
        self._tooltip_iid = None
        # Dedicated tooltip for the ETF-self constituent hover (separate
        # from the tree tooltip above so the two never fight over state).
        self._etf_tip_win = None
        self.tree.bind("<Motion>", self._on_tree_hover)
        self.tree.bind("<Leave>", lambda e: self._hide_tooltip())
        
        self.lbl_credit = tk.Label(self, text="Morning Scanner", bg=self.colors["BG"], fg=self.colors["CREDIT"])
        self.lbl_credit.pack(side="bottom", pady=(0, 2))

        self.apply_theme()
        self.adjust_font(0)
        self.var_earnings.set(self._pending_show_earnings)
        self.var_48.set(self._pending_show_48)
        self.var_all.set(self._pending_show_all)
        self.var_float.set(self._pending_show_float)
        self.var_rvol.set(self._pending_show_rvol)
        # Push loaded Finviz throttle into the live fetcher.
        self.fetcher.finviz_min_interval = self._pending_finviz_min_interval
        # Float coloration — cutoff lives on the fetcher (used by
        # parse_float); the two colors live on the app (used at render).
        self.fetcher.float_low_threshold = self._pending_float_low_threshold
        self.float_low_color = self._pending_float_low_color
        self.float_high_color = self._pending_float_high_color
        # Float coloration on/off (Settings dialog) — when off, Float
        # renders in the theme's default fg instead of low/high colors.
        self.float_color_enabled = self._pending_float_color_enabled
        # Market-cap stepped gradient: on/off + the five tier colors.
        self.mcap_gradient_enabled = self._pending_mcap_gradient_enabled
        self.mcap_tier_colors = dict(self._pending_mcap_tier_colors)
        # Earnings live attrs — read by refresh_meta_label.
        self.earn_past_days = self._pending_earn_past_days
        self.earn_future_days = self._pending_earn_future_days
        self.earn_future_color = self._pending_earn_future_color
        self.earn_pos_color = self._pending_earn_pos_color
        self.earn_neg_color = self._pending_earn_neg_color
        self.earnings_db_path = self._pending_earnings_db_path
        self.earnings_chart_font_mult = self._pending_earnings_chart_font_mult
        self.earnings_chart_geometry = self._pending_earnings_chart_geometry
        self.earnings_chart_maximized = self._pending_earnings_chart_maximized
        # Commit the earnings-chart popup color overrides.
        for _attr, _val in self._pending_chart_colors.items():
            setattr(self, _attr, _val)
        self.historical_forms = self._pending_historical_forms
        self.historical_polygon_max_tickers = self._pending_historical_polygon_max_tickers
        # SEC contact: a saved Settings value wins; blank falls back to the
        # import-time env var, then the placeholder. Apply to the live UA
        # globals now (before the first SEC fetch on symbol change) and warn
        # once if we're still on the placeholder.
        self.sec_contact = self._pending_sec_contact
        _set_sec_contact(self.sec_contact
                         or os.environ.get("MS_SEC_CONTACT", "").strip())
        if _SEC_CONTACT_IS_PLACEHOLDER:
            _log.warning("SEC User-Agent uses the placeholder contact (%s) — "
                         "set a real email in Settings → SEC access (or the "
                         "MS_SEC_CONTACT env var) if SEC throttles requests",
                         _SEC_CONTACT_PLACEHOLDER)
        if self._pending_search_kw:
            self.entry_search_kw.insert(0, self._pending_search_kw)
        if self._pending_search_date:
            self.entry_search_date.insert(0, self._pending_search_date)
        if self._pending_hot_words_new:
            self.entry_hot_new.insert(0, self._pending_hot_words_new)
            self.hot_words_new = self._parse_hot_words(self._pending_hot_words_new)
        if self._pending_hot_words_old:
            self.entry_hot_old.insert(0, self._pending_hot_words_old)
            self.hot_words_old = self._parse_hot_words(self._pending_hot_words_old)
        if self._pending_maximized:
            self.state("zoomed")

        self.current_items = []
        self._displayed_indices = []
        self._fetch_gen = 0
        # Single lock for ALL generation counters (_fetch_gen,
        # _earnings_yoy_gen, _historical_gen). Today all mutations
        # happen on the Tk thread so atomicity is already implicit,
        # but wrapping makes the code forward-safe if a daemon thread
        # ever needs to bump one. Reads stay lock-free (int read is
        # atomic in CPython).
        self._gen_lock = threading.Lock()

        # Search filter must be parsed *after* current_items exists so
        # apply_search → refresh_ui doesn't trip on a missing attr.
        if self._pending_search_kw or self._pending_search_date:
            self.apply_search()
        if self._pending_search_visible:
            self._toggle_search_row()
        self.debounce_timer = None
        self.bind("<Configure>", self._on_resize)

        # Build the ETF map after settings have been loaded so a
        # user-defined custom path is honored. Falls back to the
        # alongside-the-exe default + bundled baseline if the custom
        # path is empty or unreadable.
        self.etf_map_custom_path = self._pending_etf_map_custom_path or ""
        etf_path = (
            Path(self.etf_map_custom_path)
            if self.etf_map_custom_path
            else ETF_MAP_DEFAULT_PATH
        )
        try:
            self.etf_map = EtfMap(path=etf_path)
        except Exception as exc:  # noqa: BLE001 — defensive: never block startup
            _log.warning("ETF map init failed (%s); indicator disabled",
                          type(exc).__name__)
            self.etf_map = None
        # Multi-holding ETF holdings map (sector/index/thematic + leveraged
        # index funds). Independent of the single-stock map and always at
        # its default alongside-the-exe location. Drives the second "Held"
        # indicator (stock -> ETFs holding it) and the ETF-self tooltip.
        try:
            self.etf_holdings = EtfHoldings()
        except Exception as exc:  # noqa: BLE001
            _log.warning("ETF holdings init failed (%s); held indicator disabled",
                          type(exc).__name__)
            self.etf_holdings = None
        # Initial paint of the indicator (will be no-op until a symbol
        # is picked up by the watcher).
        self._update_etf_label(self.current_symbol)

        # Kick the parquet load onto a daemon thread BEFORE starting
        # the watch / status loops so the first symbol-change tick
        # doesn't block on the read. _get_earnings_db_full() returns
        # None until this finishes, at which point _on_parquet_loaded
        # marshals back to repaint the meta row.
        self._earnings_db_full_cache = self._PARQUET_NOT_LOADED
        self._earnings_db_mtime = None      # mtime of the loaded parquet
        self._parquet_reloading = False     # guard: a reload is in flight
        threading.Thread(
            target=self._async_load_parquet, daemon=True,
            name="MS-ParquetLoad",
        ).start()

        self.watch_thread = WatchThread(initial_mode=self.watch_mode.get())
        self.after(200, self.watch_loop)
        self.after(1000, self.status_loop)
        # Pick up earnings_pipeline parquet refreshes without a restart.
        self.after(self._PARQUET_POLL_MS, self._poll_parquet_freshness)

    def _on_resize(self, event):
        # Root-window size changes — wrap the purple Finviz summary line to fit.
        if event.widget is self:
            width = max(100, self.winfo_width() - 30)
            self.lbl_name.configure(wraplength=width)

    def toggle_theme(self):
        self.theme_mode = "light" if self.theme_mode == "dark" else "dark"
        self.colors = THEMES[self.theme_mode]
        self.apply_theme()
        self.refresh_ui()

    def open_settings_dialog(self):
        """Settings window. Edits Finviz throttle + earnings windows
        and colors. Live-applies on Save and immediately re-renders
        the meta label so the user sees the color change."""
        dlg = tk.Toplevel(self)
        dlg.title("Settings")
        dlg.transient(self)
        dlg.configure(bg=self.colors["BG"])
        dlg.resizable(False, False)
        try:
            dlg.attributes("-topmost", True)
        except tk.TclError:
            pass

        c = self.colors
        lbl_conf = {"bg": c["BG"], "fg": c["FG"]}
        ent_conf = {"bg": c["ENTRY_BG"], "fg": c["ENTRY_FG"], "insertbackground": c["ENTRY_FG"]}
        btn_conf = {"bg": c["BTN_BG"], "fg": c["BTN_FG"]}
        std = ("Segoe UI", self.base_font_size)
        std_bold = ("Segoe UI", self.base_font_size, "bold")
        small = ("Segoe UI", max(7, self.base_font_size - 1))

        wrap = tk.Frame(dlg, bg=c["BG"])
        wrap.pack(fill="both", expand=True, padx=12, pady=10)

        rng_lo, rng_hi = MIN_SCRAPE_INTERVAL_RANGE

        # ----- Finviz section -----
        tk.Label(wrap, text="Finviz", font=std_bold, **lbl_conf).grid(
            row=0, column=0, sticky="w", pady=(0, 4),
        )
        tk.Label(wrap, text="Request interval (sec)", font=std, **lbl_conf).grid(
            row=1, column=0, sticky="w", padx=(12, 6),
        )
        var_throttle = tk.StringVar(value=f"{self.fetcher.finviz_min_interval:.2f}")
        ent_throttle = tk.Entry(wrap, textvariable=var_throttle, width=8, font=std, **ent_conf)
        ent_throttle.grid(row=1, column=1, sticky="w")
        tk.Label(
            wrap, text=f"  ({rng_lo:g}–{rng_hi:g}; lower = faster, more rate-limit risk)",
            font=small, bg=c["BG"], fg=c["CREDIT"],
        ).grid(row=1, column=2, columnspan=3, sticky="w")

        # ----- Earnings section -----
        tk.Label(wrap, text="Earnings", font=std_bold, **lbl_conf).grid(
            row=2, column=0, sticky="w", pady=(12, 4),
        )

        tk.Label(wrap, text="Past window (days)", font=std, **lbl_conf).grid(
            row=3, column=0, sticky="w", padx=(12, 6),
        )
        var_past = tk.StringVar(value=str(self.earn_past_days))
        ent_past = tk.Entry(wrap, textvariable=var_past, width=6, font=std, **ent_conf)
        ent_past.grid(row=3, column=1, sticky="w")
        tk.Label(
            wrap, text="  (0–60; today + previous N days color the Earn date by EPS-surprise sign)",
            font=small, bg=c["BG"], fg=c["CREDIT"],
        ).grid(row=3, column=2, columnspan=3, sticky="w")

        tk.Label(wrap, text="Future window (days)", font=std, **lbl_conf).grid(
            row=4, column=0, sticky="w", padx=(12, 6),
        )
        var_future = tk.StringVar(value=str(self.earn_future_days))
        ent_future = tk.Entry(wrap, textvariable=var_future, width=6, font=std, **ent_conf)
        ent_future.grid(row=4, column=1, sticky="w")
        tk.Label(
            wrap, text="  (0–60; next N days color the Earn date with Future color; values are suppressed for future events)",
            font=small, bg=c["BG"], fg=c["CREDIT"],
        ).grid(row=4, column=2, columnspan=3, sticky="w")

        def make_color_row(row, label_text, initial_value, hint):
            tk.Label(wrap, text=label_text, font=std, **lbl_conf).grid(
                row=row, column=0, sticky="w", padx=(12, 6),
            )
            var = tk.StringVar(value=initial_value)
            ent = tk.Entry(wrap, textvariable=var, width=10, font=std, **ent_conf)
            ent.grid(row=row, column=1, sticky="w")
            swatch = tk.Label(wrap, text="    ", bg=initial_value, width=3)
            swatch.grid(row=row, column=2, sticky="w", padx=(6, 6))
            tk.Label(wrap, text=hint, font=small, bg=c["BG"], fg=c["CREDIT"]).grid(
                row=row, column=3, sticky="w",
            )
            # Live preview swatch as the user types a valid #RRGGBB
            def update_swatch(*_):
                v = var.get().strip()
                if self._is_valid_hex_color(v):
                    try: swatch.config(bg=v)
                    except tk.TclError: pass
            var.trace_add("write", update_swatch)
            return var

        var_color_future = make_color_row(5, "Future color (#hex)", self.earn_future_color,
                                          "  Earn date when upcoming (within future window)")
        var_color_pos = make_color_row(6, "Past pos color (#hex)", self.earn_pos_color,
                                        "  Earn date when most recent EPS surprise > 0 (within past window)")
        var_color_neg = make_color_row(7, "Past neg color (#hex)", self.earn_neg_color,
                                        "  Earn date when most recent EPS surprise < 0 (within past window)")

        # ----- Earnings chart DB section -----
        tk.Label(wrap, text="Earnings chart", font=std_bold, **lbl_conf).grid(
            row=8, column=0, sticky="w", pady=(12, 4),
        )
        tk.Label(wrap, text="History parquet path", font=std, **lbl_conf).grid(
            row=9, column=0, sticky="w", padx=(12, 6),
        )
        var_db_path = tk.StringVar(value=self.earnings_db_path)
        ent_db_path = tk.Entry(wrap, textvariable=var_db_path, width=60, font=small, **ent_conf)
        ent_db_path.grid(row=9, column=1, columnspan=3, sticky="we")
        tk.Label(
            wrap,
            text="  source for the double-click-Earn chart popup; use the earnings_pipeline earnings_history.parquet",
            font=small, bg=c["BG"], fg=c["CREDIT"],
        ).grid(row=10, column=0, columnspan=4, sticky="w", padx=(12, 0))

        # ----- Historical lookup section (Polygon + EDGAR) -----
        tk.Label(wrap, text="Historical lookup", font=std_bold, **lbl_conf).grid(
            row=11, column=0, sticky="w", pady=(12, 4),
        )

        # API keys subframe — set / clear / check the Polygon key in
        # the OS keyring (Windows Credential Manager). The key itself
        # never lives in scanner_settings.json; only its presence is
        # ever surfaced to the dialog.
        api_box = tk.LabelFrame(
            wrap, text="API Keys",
            bg=c["BG"], fg=c["FG"], font=std,
            labelanchor="nw", padx=8, pady=6,
        )
        api_box.grid(row=12, column=0, columnspan=4, sticky="we",
                      padx=(12, 0), pady=(0, 4))

        tk.Label(api_box, text="Polygon API key", font=std,
                  bg=c["BG"], fg=c["FG"]).grid(
            row=0, column=0, sticky="w", padx=(0, 6),
        )
        var_poly_key = tk.StringVar(value="")
        ent_poly_key = tk.Entry(
            api_box, textvariable=var_poly_key, width=44, font=small,
            show="•", **ent_conf,
        )
        ent_poly_key.grid(row=0, column=1, sticky="we", padx=(0, 6))
        api_status = tk.Label(
            api_box, text="", font=small, bg=c["BG"], fg=c["CREDIT"],
        )
        api_status.grid(row=1, column=1, sticky="w", padx=(0, 6),
                         pady=(2, 0))

        def refresh_api_status():
            existing = _keyring_get_polygon()
            if not _HAS_KEYRING:
                api_status.config(
                    text="keyring package missing — install + restart",
                    fg=c["TXT_BAD"],
                )
            elif existing:
                # Show only the tail to confirm presence without
                # leaking the full key into the dialog.
                tail = existing[-4:] if len(existing) > 4 else "****"
                api_status.config(
                    text=f"✓ key on file (…{tail})",
                    fg=c["TXT_OK"],
                )
            else:
                api_status.config(text="✗ no key set", fg=c["TXT_BAD"])

        def save_poly_key():
            v = var_poly_key.get().strip()
            if not v:
                api_status.config(text="enter a key first", fg=c["TXT_BAD"])
                return
            if _keyring_set_polygon(v):
                var_poly_key.set("")
                refresh_api_status()
            else:
                api_status.config(text="failed to save (keyring error)",
                                    fg=c["TXT_BAD"])

        def clear_poly_key():
            if _keyring_clear_polygon():
                refresh_api_status()
            else:
                # Either keyring missing OR no key existed — refresh
                # status so the user sees the current state regardless.
                refresh_api_status()

        btn_poly_save = tk.Button(api_box, text="Save", command=save_poly_key,
                                    font=std, **btn_conf)
        btn_poly_save.grid(row=0, column=2, padx=(0, 4))
        btn_poly_check = tk.Button(api_box, text="Check",
                                     command=refresh_api_status,
                                     font=std, **btn_conf)
        btn_poly_check.grid(row=0, column=3, padx=(0, 4))
        btn_poly_clear = tk.Button(api_box, text="Clear",
                                     command=clear_poly_key,
                                     font=std, **btn_conf)
        btn_poly_clear.grid(row=0, column=4)
        api_box.grid_columnconfigure(1, weight=1)
        refresh_api_status()

        # Tunables for the Historical lookup itself.
        tk.Label(wrap, text="EDGAR forms", font=std, **lbl_conf).grid(
            row=13, column=0, sticky="w", padx=(12, 6),
        )
        var_hist_forms = tk.StringVar(
            value=getattr(self, "historical_forms", DEFAULT_HISTORICAL_FORMS),
        )
        ent_hist_forms = tk.Entry(
            wrap, textvariable=var_hist_forms, width=60, font=small, **ent_conf,
        )
        ent_hist_forms.grid(row=13, column=1, columnspan=3, sticky="we")
        tk.Label(
            wrap,
            text=f"  comma-separated; default = {DEFAULT_HISTORICAL_FORMS}",
            font=small, bg=c["BG"], fg=c["CREDIT"],
        ).grid(row=14, column=0, columnspan=4, sticky="w", padx=(12, 0))

        tk.Label(wrap, text="Polygon max tickers", font=std, **lbl_conf).grid(
            row=15, column=0, sticky="w", padx=(12, 6),
        )
        var_hist_max_tickers = tk.StringVar(
            value=str(getattr(self, "historical_polygon_max_tickers", 5)),
        )
        ent_hist_max_tickers = tk.Entry(
            wrap, textvariable=var_hist_max_tickers, width=6, font=std, **ent_conf,
        )
        ent_hist_max_tickers.grid(row=15, column=1, sticky="w")
        tk.Label(
            wrap,
            text="  drop articles tagged with more than N tickers (0 = no filter; default 5)",
            font=small, bg=c["BG"], fg=c["CREDIT"],
        ).grid(row=15, column=2, columnspan=2, sticky="w")

        # ----- Single-stock ETF map -----
        tk.Label(wrap, text="Single-stock ETF map", font=std_bold, **lbl_conf).grid(
            row=16, column=0, sticky="w", pady=(12, 4),
        )
        tk.Label(wrap, text="Map path (custom)", font=std, **lbl_conf).grid(
            row=17, column=0, sticky="w", padx=(12, 6),
        )
        var_etf_path = tk.StringVar(value=str(self.etf_map_custom_path or ""))
        ent_etf_path = tk.Entry(
            wrap, textvariable=var_etf_path, width=60, font=small, **ent_conf,
        )
        ent_etf_path.grid(row=17, column=1, columnspan=3, sticky="we")
        # Show the *effective* path (resolved against the running EtfMap)
        # so the user can see which file the indicator is reading from.
        effective_path = (
            str(self.etf_map.path) if self.etf_map is not None
            else str(ETF_MAP_DEFAULT_PATH)
        )
        tk.Label(
            wrap,
            text=f"  blank = use default ({ETF_MAP_DEFAULT_PATH.name} next to exe). "
                 f"Currently: {effective_path}",
            font=small, bg=c["BG"], fg=c["CREDIT"],
        ).grid(row=18, column=0, columnspan=4, sticky="w", padx=(12, 0))

        etf_btn_row = tk.Frame(wrap, bg=c["BG"])
        etf_btn_row.grid(row=19, column=0, columnspan=4, sticky="w", padx=(12, 0), pady=(4, 0))
        tk.Button(
            etf_btn_row, text="Refresh now",
            command=lambda: self._open_etf_refresh_dialog(var_etf_path.get().strip()),
            font=std, **btn_conf,
        ).pack(side="left")
        tk.Button(
            etf_btn_row, text="Health",
            command=lambda: self._open_etf_health_dialog(var_etf_path.get().strip()),
            font=std, **btn_conf,
        ).pack(side="left", padx=(6, 0))

        # ----- SEC access -----
        tk.Label(wrap, text="SEC access", font=std_bold, **lbl_conf).grid(
            row=20, column=0, sticky="w", pady=(12, 4),
        )
        tk.Label(wrap, text="Contact email", font=std, **lbl_conf).grid(
            row=21, column=0, sticky="w", padx=(12, 6),
        )
        var_sec_contact = tk.StringVar(value=str(getattr(self, "sec_contact", "") or ""))
        ent_sec_contact = tk.Entry(
            wrap, textvariable=var_sec_contact, width=40, font=small, **ent_conf,
        )
        ent_sec_contact.grid(row=21, column=1, columnspan=3, sticky="we")
        tk.Label(
            wrap,
            text="  declared in the SEC User-Agent (fair-access). Blank = "
                 "non-deliverable placeholder; SEC may throttle.",
            font=small, bg=c["BG"], fg=c["CREDIT"],
        ).grid(row=22, column=0, columnspan=4, sticky="w", padx=(12, 0))

        # ----- Float coloration -----
        tk.Label(wrap, text="Float", font=std_bold, **lbl_conf).grid(
            row=23, column=0, sticky="w", pady=(12, 4),
        )
        tk.Label(wrap, text="Low-float cutoff (M shares)", font=std, **lbl_conf).grid(
            row=24, column=0, sticky="w", padx=(12, 6),
        )
        var_float_cut = tk.StringVar(
            value=f"{self.fetcher.float_low_threshold / 1_000_000:g}")
        ent_float_cut = tk.Entry(wrap, textvariable=var_float_cut, width=8,
                                 font=std, **ent_conf)
        ent_float_cut.grid(row=24, column=1, sticky="w")
        flo_m, fhi_m = LOW_FLOAT_RANGE_M
        tk.Label(
            wrap, text=f"  float below this (in millions) shows the low color "
                       f"({flo_m:g}–{fhi_m:g})",
            font=small, bg=c["BG"], fg=c["CREDIT"],
        ).grid(row=24, column=2, columnspan=2, sticky="w")

        def make_float_color_row(row, label_text, stored, theme_color, hint):
            tk.Label(wrap, text=label_text, font=std, **lbl_conf).grid(
                row=row, column=0, sticky="w", padx=(12, 6))
            var = tk.StringVar(value=stored)  # "" (= follow theme) or #RRGGBB
            ent = tk.Entry(wrap, textvariable=var, width=10, font=std, **ent_conf)
            ent.grid(row=row, column=1, sticky="w")
            swatch = tk.Label(wrap, text="    ", bg=theme_color, width=3)
            swatch.grid(row=row, column=2, sticky="w", padx=(6, 6))
            tk.Label(wrap, text=hint, font=small, bg=c["BG"], fg=c["CREDIT"]).grid(
                row=row, column=3, sticky="w")

            def upd(*_):
                v = var.get().strip()
                eff = v if self._is_valid_hex_color(v) else theme_color
                try: swatch.config(bg=eff)
                except tk.TclError: pass
            var.trace_add("write", upd)
            return var

        var_float_low = make_float_color_row(
            25, "Low color (#hex)", getattr(self, "float_low_color", "") or "",
            c["TXT_OK"], "  blank = theme green")
        var_float_high = make_float_color_row(
            26, "High color (#hex)", getattr(self, "float_high_color", "") or "",
            c["TXT_BAD"], "  blank = theme red")

        dlg_cb_conf = {"bg": c["BG"], "fg": c["FG"], "selectcolor": c["BG"],
                       "activebackground": c["BG"], "activeforeground": c["FG"]}
        var_float_color_en = tk.BooleanVar(
            value=bool(getattr(self, "float_color_enabled", True)))
        tk.Checkbutton(
            wrap, text="Color the Float value (low/high)",
            variable=var_float_color_en, font=std, **dlg_cb_conf,
        ).grid(row=27, column=0, columnspan=3, sticky="w", padx=(12, 0))

        # ----- Market Cap gradient section -----
        tk.Label(wrap, text="Market Cap", font=std_bold, **lbl_conf).grid(
            row=28, column=0, sticky="w", pady=(12, 4),
        )
        var_mcap_grad_en = tk.BooleanVar(
            value=bool(getattr(self, "mcap_gradient_enabled", True)))
        tk.Checkbutton(
            wrap, text="Color MCap by tier (stepped gradient)",
            variable=var_mcap_grad_en, font=std, **dlg_cb_conf,
        ).grid(row=29, column=0, columnspan=3, sticky="w", padx=(12, 0))

        stored_tiers = getattr(self, "mcap_tier_colors", None) or MCAP_TIER_DEFAULT_COLORS
        var_mcap_tiers = {}
        for i, tier in enumerate(MCAP_TIER_KEYS):
            var_mcap_tiers[tier] = make_float_color_row(
                30 + i, f"{MCAP_TIER_LABELS[tier]} (#hex)",
                stored_tiers.get(tier, MCAP_TIER_DEFAULT_COLORS[tier]),
                MCAP_TIER_DEFAULT_COLORS[tier],
                f"  blank = default {MCAP_TIER_DEFAULT_COLORS[tier]}")

        err_lbl = tk.Label(wrap, text="", bg=c["BG"], fg=c["TXT_BAD"], font=std)
        err_lbl.grid(row=35, column=0, columnspan=4, sticky="w", pady=(8, 0))

        def save_and_close():
            # Validate SEC contact (blank is allowed = placeholder/env fallback)
            sec_contact_val = var_sec_contact.get().strip()
            if sec_contact_val and not _is_valid_sec_contact(sec_contact_val):
                err_lbl.config(text="SEC contact must be a valid email (or blank)")
                return
            # Validate Finviz throttle
            try:
                throttle_val = float(var_throttle.get().strip())
            except ValueError:
                err_lbl.config(text="Finviz interval must be a number")
                return
            if not (rng_lo <= throttle_val <= rng_hi):
                err_lbl.config(text=f"Finviz interval must be {rng_lo:g}–{rng_hi:g}")
                return
            # Validate float cutoff (entered in millions of shares) + colors.
            try:
                float_cut_m = float(var_float_cut.get().strip())
            except ValueError:
                err_lbl.config(text="Float cutoff must be a number (millions of shares)")
                return
            if not (flo_m <= float_cut_m <= fhi_m):
                err_lbl.config(text=f"Float cutoff must be {flo_m:g}–{fhi_m:g} (M shares)")
                return
            float_low_val = var_float_low.get().strip()
            float_high_val = var_float_high.get().strip()
            for lbl_name, cval in (("Float low", float_low_val),
                                   ("Float high", float_high_val)):
                if cval and not self._is_valid_hex_color(cval):
                    err_lbl.config(text=f"{lbl_name} color must be #RRGGBB (or blank)")
                    return
            # MCap tier colors — blank reverts to that tier's default.
            mcap_tiers_resolved = {}
            for tier in MCAP_TIER_KEYS:
                tval = var_mcap_tiers[tier].get().strip()
                if tval and not self._is_valid_hex_color(tval):
                    err_lbl.config(
                        text=f"{MCAP_TIER_LABELS[tier]} color must be #RRGGBB (or blank)")
                    return
                mcap_tiers_resolved[tier] = tval or MCAP_TIER_DEFAULT_COLORS[tier]
            # Validate windows
            try:
                past_d = int(var_past.get().strip())
                future_d = int(var_future.get().strip())
            except ValueError:
                err_lbl.config(text="Past/Future days must be integers")
                return
            if not (0 <= past_d <= 60) or not (0 <= future_d <= 60):
                err_lbl.config(text="Past/Future days must be 0–60")
                return
            # Validate colors
            color_future = var_color_future.get().strip()
            color_pos = var_color_pos.get().strip()
            color_neg = var_color_neg.get().strip()
            for label, val in (("Future", color_future),
                                ("Past pos", color_pos),
                                ("Past neg", color_neg)):
                if not self._is_valid_hex_color(val):
                    err_lbl.config(text=f"{label} color must be #RRGGBB")
                    return
            # Apply
            self.fetcher.finviz_min_interval = throttle_val
            # Float coloration — cutoff (millions -> shares) + colors.
            self.fetcher.float_low_threshold = float_cut_m * 1_000_000
            self.float_low_color = float_low_val
            self.float_high_color = float_high_val
            self.float_color_enabled = bool(var_float_color_en.get())
            self.mcap_gradient_enabled = bool(var_mcap_grad_en.get())
            self.mcap_tier_colors = mcap_tiers_resolved
            self.earn_past_days = past_d
            self.earn_future_days = future_d
            self.earn_future_color = color_future
            self.earn_pos_color = color_pos
            self.earn_neg_color = color_neg
            # Earnings DB path — accept whatever the user typed; the
            # chart popup itself reports a friendly error if the file
            # doesn't load. Don't gate Save on file existence so a
            # path entered before the file is generated still saves.
            self.earnings_db_path = var_db_path.get().strip() or DEFAULT_EARNINGS_DB_PATH
            # Validate historical lookup tunables.
            forms_val = var_hist_forms.get().strip() or DEFAULT_HISTORICAL_FORMS
            try:
                max_tk = int(var_hist_max_tickers.get().strip())
            except ValueError:
                err_lbl.config(text="Polygon max tickers must be an integer")
                return
            if not (0 <= max_tk <= 100):
                err_lbl.config(text="Polygon max tickers must be 0–100")
                return
            self.historical_forms = forms_val
            self.historical_polygon_max_tickers = max_tk
            # SEC contact — apply to the live UA globals immediately so the
            # next SEC request uses it. Blank reverts to the env var / placeholder.
            self.sec_contact = sec_contact_val
            _set_sec_contact(sec_contact_val
                             or os.environ.get("MS_SEC_CONTACT", "").strip())
            # ETF map path. Empty string = use default alongside-the-exe
            # location. If the user typed a non-blank path, push it to
            # the running EtfMap, which reloads + falls back to the
            # bundled baseline if the file isn't readable.
            new_etf_path = var_etf_path.get().strip()
            self.etf_map_custom_path = new_etf_path
            if self.etf_map is not None:
                target = Path(new_etf_path) if new_etf_path else ETF_MAP_DEFAULT_PATH
                try:
                    self.etf_map.set_path(target)
                except Exception as exc:  # noqa: BLE001
                    err_lbl.config(text=f"ETF map path: {exc}")
                    return
                self._update_etf_label(self.current_symbol)
            # Invalidate both earnings caches so the next read goes
            # through to the new path.
            self._earnings_tickers_cache = None
            # Invalidate + re-load on the background thread so the
            # Settings dialog Save doesn't block while the new parquet
            # path is read. _get_earnings_db_full() returns None in the
            # interim and the meta row repaints on completion.
            self._earnings_db_full_cache = self._PARQUET_NOT_LOADED
            # Mark a reload in flight so a concurrent _poll_parquet_freshness
            # tick doesn't launch a SECOND overlapping loader (#15).
            self._parquet_reloading = True
            threading.Thread(
                target=self._async_load_parquet, daemon=True,
                name="MS-ParquetLoad",
            ).start()
            # Re-render meta label so the new colors / windows show
            # immediately without waiting for the next refresh.
            try: self.refresh_meta_label()
            except Exception: pass
            # Re-render the MCap + Float header labels live with the new
            # cutoff / colors / toggles (they're painted in scrape_sec_data,
            # not refresh_meta_label, so recompute is_low for the current
            # symbol and repaint both here).
            try:
                m = self.current_meta or {}
                if m.get("float"):
                    _, is_low = self.fetcher.parse_float(m["float"])
                    m["is_low"] = is_low
                self._render_mcap_label()
                self._render_float_label()
            except Exception:
                pass
            # Persist the dialog's settings to disk NOW (not just in
            # on_close) so a later force-kill can't silently revert them
            # (#18). Best-effort merge so it never blocks the Save.
            self._merge_persist_settings({
                "finviz_min_interval": float(self.fetcher.finviz_min_interval),
                "float_low_threshold": float(self.fetcher.float_low_threshold),
                "float_low_color": str(getattr(self, "float_low_color", "") or ""),
                "float_high_color": str(getattr(self, "float_high_color", "") or ""),
                "float_color_enabled": bool(self.float_color_enabled),
                "mcap_gradient_enabled": bool(self.mcap_gradient_enabled),
                "mcap_tier_colors": {k: str(v) for k, v in self.mcap_tier_colors.items()},
                "earn_past_days": int(self.earn_past_days),
                "earn_future_days": int(self.earn_future_days),
                "earn_future_color": str(self.earn_future_color),
                "earn_pos_color": str(self.earn_pos_color),
                "earn_neg_color": str(self.earn_neg_color),
                "earnings_db_path": str(self.earnings_db_path),
                "historical_forms": str(getattr(
                    self, "historical_forms", DEFAULT_HISTORICAL_FORMS)),
                "historical_polygon_max_tickers": int(getattr(
                    self, "historical_polygon_max_tickers", 5)),
                "etf_map_custom_path": str(getattr(
                    self, "etf_map_custom_path", "") or ""),
                "sec_contact": str(getattr(self, "sec_contact", "") or ""),
            })
            dlg.destroy()

        btn_row = tk.Frame(wrap, bg=c["BG"])
        btn_row.grid(row=36, column=0, columnspan=4, sticky="e", pady=(12, 0))
        tk.Button(btn_row, text="Cancel", command=dlg.destroy, font=std, **btn_conf).pack(
            side="right", padx=(6, 0),
        )
        tk.Button(btn_row, text="Save", command=save_and_close, font=std, **btn_conf).pack(
            side="right",
        )

        ent_throttle.focus_set()
        ent_throttle.select_range(0, "end")
        dlg.bind("<Return>", lambda e: save_and_close())
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    def apply_theme(self):
        c = self.colors
        self.configure(bg=c["BG"])
        self.style.configure("Treeview", background=c["BG"], foreground=c["FG"], fieldbackground=c["BG"])
        self.style.configure("Treeview.Heading", background=c["ACCENT"], foreground=c["FG"])
        self.style.map("Treeview", background=[("selected", c["TREE_SEL"])], foreground=[("selected", c["FG"])])
        self.tree.tag_configure("hot_new", foreground=c["TXT_OK"])
        self.tree.tag_configure("hot_old", foreground=c["TXT_BAD"])
        self.tree.tag_configure("today", foreground=c["FG"])
        self.tree.tag_configure("old", foreground=c["TREE_OLD"])
        # Historical-mode row tags. Sentiment-keyed colors for Polygon
        # rows; EDGAR rows take the standard fg; banner uses the same
        # purple as the chart highlight; loading/notes muted grey.
        self.tree.tag_configure("hist_banner", foreground="#9C27B0")
        self.tree.tag_configure("hist_loading", foreground=c["CREDIT"])
        self.tree.tag_configure("hist_note", foreground=c["TREE_OLD"])
        self.tree.tag_configure("hist_default", foreground=c["FG"])
        self.tree.tag_configure("hist_edgar", foreground=c["FG"])
        self.tree.tag_configure("hist_wires", foreground=c["FG"])
        self.tree.tag_configure("hist_pos", foreground=c["TXT_OK"])
        self.tree.tag_configure("hist_neg", foreground=c["TXT_BAD"])
        self.tree.tag_configure("hist_neu", foreground=c["FG"])
        # YoY-enriched 10-K/10-Q rows. Tag colors the entire row's text
        # since stock ttk Treeview can't do per-cell colors. Mixed
        # (one pos, one neg) falls back to the standard fg.
        # Match the earnings-chart popup's YoY palette (deep sky blue
        # for positive, deep pink for negative). Treeview only supports
        # whole-row coloring so the row is tinted by the YoY EPS sign;
        # the tooltip renders the full per-value breakdown when hovered.
        self.tree.tag_configure("hist_yoy_pos", foreground="#00BFFF")
        self.tree.tag_configure("hist_yoy_neg", foreground="#FF1493")
        self.tree.tag_configure("hist_yoy_mixed", foreground=c["FG"])

        self.hdr.config(bg=c["BG"])
        self.header_top.config(bg=c["BG"])
        self.lbl_symbol.config(bg=c["BG"], fg=c["FG"])
        self.lbl_name.config(bg=c["BG"], fg=c["VIOLET"])
        self.lbl_shelf.config(bg=c["BG"])
        self.lbl_sec_recent.config(bg=c["BG"])
        self.lbl_mcap.config(bg=c["BG"])
        self.lbl_float.config(bg=c["BG"])
        # Refresh the ETF indicator under the new palette — color depends on
        # current state, so re-derive it rather than picking a single color.
        if hasattr(self, "lbl_etf"):
            self.lbl_etf.config(bg=c["BG"])
            if hasattr(self, "lbl_etf_hold"):
                self.lbl_etf_hold.config(bg=c["BG"])
            # Re-derives both the primary and the 'Held' indicator state/color.
            self._update_etf_label(self.current_symbol)
        self.lbl_meta.config(bg=c["BG"], fg=c["FG"])
        self.earnings_row.config(bg=c["BG"])
        self.lbl_earnings.config(bg=c["BG"])
        self.lbl_eps_surp.config(bg=c["BG"])
        self.lbl_sales_surp.config(bg=c["BG"])
        if hasattr(self, "lbl_eps_yoy"):
            self.lbl_eps_yoy.config(bg=c["BG"])
            self.lbl_rev_yoy.config(bg=c["BG"])

        self.ctrl.config(bg=c["BG"])
        cb_conf = {"bg": c["BG"], "fg": c["FG"], "selectcolor": c["BG"], "activebackground": c["BG"]}
        self.chk_48.config(**cb_conf)
        self.chk_all.config(**cb_conf)
        self.chk_float.config(**cb_conf)
        self.chk_rvol.config(**cb_conf)
        self.chk_earnings.config(**cb_conf)
        # Re-derive the MCap gradient + Float colors under the new palette.
        self._render_mcap_label()
        self._render_float_label()
        
        self.lbl_highlight_new.config(bg=c["BG"], fg=c["TXT_OK"])
        self.lbl_highlight_old.config(bg=c["BG"], fg=c["TXT_BAD"])
        self.entry_hot_new.config(bg=c["ENTRY_BG"], fg=c["ENTRY_FG"], insertbackground=c["ENTRY_FG"])
        self.entry_hot_old.config(bg=c["ENTRY_BG"], fg=c["ENTRY_FG"], insertbackground=c["ENTRY_FG"])

        self.search_row.config(bg=c["BG"])
        self.lbl_search_kw.config(bg=c["BG"], fg=c["FG"])
        self.lbl_search_date.config(bg=c["BG"], fg=c["FG"])
        self.lbl_search_help.config(bg=c["BG"], fg=c["CREDIT"])
        self.lbl_search_err.config(bg=c["BG"], fg=c["TXT_BAD"])
        self.entry_search_kw.config(bg=c["ENTRY_BG"], fg=c["ENTRY_FG"], insertbackground=c["ENTRY_FG"])
        self.entry_search_date.config(bg=c["ENTRY_BG"], fg=c["ENTRY_FG"], insertbackground=c["ENTRY_FG"])

        self.historical_row.config(bg=c["BG"])
        self.lbl_historical.config(bg=c["BG"], fg=c["FG"])
        self.lbl_historical_help.config(bg=c["BG"], fg=c["CREDIT"])
        self.lbl_historical_err.config(bg=c["BG"], fg=c["TXT_BAD"])
        self.entry_historical_date.config(bg=c["ENTRY_BG"], fg=c["ENTRY_FG"], insertbackground=c["ENTRY_FG"])

        rb_conf = {"bg": c["BG"], "fg": c["FG"], "selectcolor": c["BG"], "activebackground": c["BG"], "activeforeground": c["FG"]}
        for rb in self.mode_radios.values():
            rb.config(**rb_conf)

        btn_conf = {"bg": c["BTN_BG"], "fg": c["BTN_FG"]}
        self.btn_apply_new.config(**btn_conf)
        self.btn_apply_old.config(**btn_conf)
        self.btn_plus.config(**btn_conf)
        self.btn_minus.config(**btn_conf)
        self.btn_theme.config(**btn_conf)
        self.btn_settings.config(**btn_conf)
        self.btn_refresh.config(**btn_conf)
        self.btn_search_toggle.config(**btn_conf)
        self.btn_search_apply.config(**btn_conf)
        self.btn_search_clear.config(**btn_conf)
        self.btn_historical_lookup.config(**btn_conf)
        self.btn_historical_exit.config(**btn_conf)
        self.refresh_info.config(bg=c["BG"])
        self.lbl_last_refresh.config(bg=c["BG"], fg=c["CREDIT"])
        
        self.stat_frame.config(bg=c["BG"])
        for code, (lbl, frame, box) in self.status_widgets.items():
            frame.config(bg=c["BG"])
            lbl.config(bg=c["BG"])
            
        self.lbl_credit.config(bg=c["BG"], fg=c["CREDIT"])
        self.refresh_meta_label()

    def adjust_font(self, delta):
        self.base_font_size += delta
        if self.base_font_size < 7: self.base_font_size = 7
        if self.base_font_size > 20: self.base_font_size = 20
        s = self.base_font_size
        
        self.style.configure("Treeview", font=("Segoe UI", s), rowheight=int(s*2.4))
        self.style.configure("Treeview.Heading", font=("Segoe UI", s, "bold"))
        
        self.lbl_symbol.config(font=("Segoe UI", s+14, "bold"))
        self.lbl_name.config(font=("Segoe UI", max(6, s+1)))
        self.lbl_shelf.config(font=("Segoe UI", s+2, "bold"))
        self.lbl_sec_recent.config(font=("Segoe UI", s+2, "bold"))
        self.lbl_mcap.config(font=("Segoe UI", s+4, "bold"))
        self.lbl_float.config(font=("Segoe UI", s+2))
        if hasattr(self, "lbl_etf"):
            self.lbl_etf.config(font=("Segoe UI", s+2, "bold"))
        if hasattr(self, "lbl_etf_hold"):
            self.lbl_etf_hold.config(font=("Segoe UI", s+2, "bold"))
        self.lbl_meta.config(font=("Segoe UI", s+2))
        self.lbl_earnings.config(font=("Segoe UI", s+2))
        self.lbl_eps_surp.config(font=("Segoe UI", s+2))
        self.lbl_sales_surp.config(font=("Segoe UI", s+2))
        if hasattr(self, "lbl_eps_yoy"):
            self.lbl_eps_yoy.config(font=("Segoe UI", s+2))
            self.lbl_rev_yoy.config(font=("Segoe UI", s+2))

        std = ("Segoe UI", s)
        std_bold = ("Segoe UI", s, "bold")
        self.chk_48.config(font=std); self.chk_all.config(font=std)
        self.chk_float.config(font=std); self.chk_rvol.config(font=std); self.chk_earnings.config(font=std)
        self.lbl_highlight_new.config(font=std_bold); self.lbl_highlight_old.config(font=std_bold)
        self.entry_hot_new.config(font=std); self.entry_hot_old.config(font=std)
        self.btn_apply_new.config(font=std); self.btn_apply_old.config(font=std)
        self.btn_plus.config(font=std); self.btn_minus.config(font=std); self.btn_theme.config(font=std)
        self.btn_refresh.config(font=std); self.lbl_last_refresh.config(font=std)
        self.btn_settings.config(font=std)
        self.btn_search_toggle.config(font=std)
        self.btn_search_apply.config(font=std); self.btn_search_clear.config(font=std)
        self.lbl_search_kw.config(font=std_bold); self.lbl_search_date.config(font=std_bold)
        self.entry_search_kw.config(font=std); self.entry_search_date.config(font=std)
        self.lbl_search_help.config(font=("Segoe UI", max(7, s-2)))
        self.lbl_search_err.config(font=std)
        for rb in self.mode_radios.values():
            rb.config(font=std_bold)
        
        tiny = ("Segoe UI", max(5, s-4))
        small_bold = ("Segoe UI", max(6, s-3), "bold")
        self.lbl_credit.config(font=("Segoe UI", max(6, s-2)))
        
        for code, (lbl, f, box) in self.status_widgets.items():
            lbl.config(font=small_bold); box.config(font=tiny)

    def watch_loop(self):
        # Never blocks — just reads the latest snapshot published by
        # the worker thread.
        sym, win_name = (None, None)
        if self.watch_thread is not None:
            sym, win_name = self.watch_thread.get_latest()
        if (sym and sym != self.current_symbol):
            if self.debounce_timer:
                # Guard after_cancel on a possibly-stale id (mirrors
                # _on_mode_change): a raise here must not skip the 500ms
                # reschedule below and kill the watcher for the session.
                try:
                    self.after_cancel(self.debounce_timer)
                except (tk.TclError, ValueError):
                    pass
                self.debounce_timer = None
            self.debounce_timer = self.after(150, lambda: self.change_symbol(sym, win_name))
        self.after(500, self.watch_loop)

    def _on_mode_change(self):
        # Switching modes: tell the worker thread to start polling
        # the new platform and drop the currently-tracked symbol so
        # the next tick picks up the active chart. Cancel any
        # in-flight debounce so the prior mode can't race in late.
        if self.debounce_timer:
            try: self.after_cancel(self.debounce_timer)
            except tk.TclError: pass
            self.debounce_timer = None
        self.current_symbol = None
        if self.watch_thread is not None:
            self.watch_thread.set_mode(self.watch_mode.get())

    def status_loop(self):
        rss_stats = self.fetcher.rss_worker.statuses
        c = self.colors
        # Cache the last applied color per indicator so a steady-state
        # session (5 indicators × 86400 ticks/day = ~430k redundant
        # widget updates) only fires .config() when something actually
        # changed. Cache key includes the theme so a theme switch still
        # forces a repaint.
        last = self._last_indicator_colors
        theme = self.theme_mode
        for code in ["PR", "GB", "YH"]:
            s = rss_stats.get(code)
            color = c["STATUS_WAIT"]
            if s == "OK": color = c["STATUS_OK"]
            elif s == "ERR": color = c["STATUS_ERR"]
            key = (theme, color)
            if last.get(code) != key:
                self.indicators[code].config(bg=color)
                last[code] = key

        fv_stat = self.fetcher.finviz_status
        f_color = c["STATUS_WAIT"]
        if fv_stat == "OK": f_color = c["STATUS_OK"]
        elif fv_stat == "ERR": f_color = c["STATUS_ERR"]
        key = (theme, f_color)
        if last.get("FV") != key:
            self.indicators["FV"].config(bg=f_color)
            last["FV"] = key

        sec_stat = self.fetcher.sec_status
        s_color = c["STATUS_WAIT"]
        if sec_stat == "OK": s_color = c["STATUS_OK"]
        elif sec_stat == "ERR": s_color = c["STATUS_ERR"]
        key = (theme, s_color)
        if last.get("SEC") != key:
            self.indicators["SEC"].config(bg=s_color)
            last["SEC"] = key

        # Watch-thread stall indicator: tint the symbol label background
        # amber when get_info() has been blocking >8s (typically a TITAN
        # UIA hang). Restores to the theme background when the watcher
        # resumes. Goes through the same diff cache as the other
        # indicators so steady-state ticks don't fire redundant
        # .config() calls.
        stalled = False
        if self.watch_thread is not None:
            try:
                stalled = bool(self.watch_thread.is_stalled())
            except Exception:
                stalled = False
        stall_bg = c["STATUS_STALL"] if stalled else c["BG"]
        stall_key = (theme, stall_bg)
        if last.get("WATCH_STALL") != stall_key:
            try:
                self.lbl_symbol.config(bg=stall_bg)
            except tk.TclError:
                pass
            last["WATCH_STALL"] = stall_key

        self.after(1000, self.status_loop)

    def change_symbol(self, sym, win_name):
        # Active chart symbol changed — historical mode is per-symbol,
        # so drop it on every transition. _fetch_gen bump below also
        # invalidates any in-flight historical fetch.
        if self.historical_active:
            self.exit_historical_mode()
        with self._gen_lock:
            self._fetch_gen += 1
            self._earnings_yoy_gen += 1
        self.current_symbol = sym
        self.current_window_name = win_name
        self.current_recent_earnings = None
        self.lbl_symbol.config(text=sym)
        self.lbl_name.config(text=win_name if win_name else "")
        self.lbl_shelf.config(text="Shelf: —", fg=self.colors["CREDIT"])
        self.lbl_sec_recent.config(text="SEC: —", fg=self.colors["FG"])
        self._update_etf_label(sym)
        self.lbl_mcap.config(text="")
        self.lbl_float.config(text="")
        self.lbl_meta.config(text="Loading...")
        self.lbl_earnings.config(text="")
        self.lbl_eps_surp.config(text="")
        self.lbl_sales_surp.config(text="")
        if hasattr(self, "lbl_eps_yoy"):
            self.lbl_eps_yoy.config(text="")
            self.lbl_rev_yoy.config(text="")
        self.tree.delete(*self.tree.get_children())
        self.current_cik = self.fetcher.cik_resolver.get_cik(sym, win_name)
        # Defer the wires filter into bg_fetch so the Tk thread isn't
        # blocked on the 500-row regex walk. The tree shows empty for
        # the ~100–500 ms until Finviz/SEC come back, at which point
        # update_full_data populates with the merged wires + Finviz
        # items. Subjectively imperceptible.
        self.current_items = []
        self.refresh_ui()
        gen = self._fetch_gen
        # If the SEC ticker manifest is still loading, the CIK lookup
        # above returned None and the upcoming SEC scrape would have
        # silently bailed. Schedule one retry shortly so the SEC chip
        # populates as soon as the manifest is ready (R3).
        if (self.current_cik is None
                and not self.fetcher.cik_resolver.loaded):
            self.after(1000, lambda s=sym, g=gen: self._retry_cik_resolve(s, g))
        threading.Thread(target=self.bg_fetch, args=(sym, gen), daemon=True).start()

    def _retry_cik_resolve(self, sym, gen):
        # If a newer symbol change has come along, drop this retry.
        if gen != self._fetch_gen or sym != self.current_symbol:
            return
        if not self.fetcher.cik_resolver.loaded:
            self.after(1000, lambda: self._retry_cik_resolve(sym, gen))
            return
        cik = self.fetcher.cik_resolver.get_cik(sym, self.current_window_name)
        if cik and cik != self.current_cik:
            self.current_cik = cik
            with self._gen_lock:
                self._fetch_gen += 1
                new_gen = self._fetch_gen
            threading.Thread(
                target=self.bg_fetch, args=(sym, new_gen), daemon=True,
            ).start()

    def bg_fetch(self, sym, gen):
        if gen != self._fetch_gen: return
        future_meta = self.fetcher.submit(self.fetcher.scrape_finviz, sym)
        future_sec = self.fetcher.submit(
            self.fetcher.scrape_sec_data, sym, self.current_cik,
        )
        # Guard the blocking result() calls. The scrapers catch only
        # narrow tuples, so an unanticipated exception (a non-string/None
        # in untrusted SEC JSON, an index slip, a bad strptime) would
        # re-raise here, silently kill this daemon thread (windowed exe ->
        # no stderr), and never schedule update_full_data — stranding the
        # row on "Loading...". Fall back to empty/default meta (matching
        # _do_manual_refresh) so the row always clears. The underlying
        # request timeouts (10-20s) already bound how long result() waits.
        try:
            meta, fv_items = future_meta.result()
            has_s3, sec_recent_status, recent_earnings = future_sec.result()
        except Exception as exc:
            _log.warning("bg_fetch failed for %s: %s", sym, type(exc).__name__)
            meta, fv_items, has_s3, sec_recent_status, recent_earnings = \
                {}, [], False, 2, None
        # Filter the in-memory wires cache for the active symbol on this
        # daemon thread instead of the Tk main thread (audit: 500-row
        # regex walk was previously paid synchronously in change_symbol).
        try:
            wires = self.fetcher.get_wires(sym)
        except Exception:
            wires = []

        if gen != self._fetch_gen: return
        # Tk isn't thread-safe; on app close this after() can race
        # destroy() and raise "main thread is not in main loop". Swallow
        # that benign teardown race (mirrors the historical-lookup worker).
        try:
            self.after(0, lambda: self.update_full_data(
                sym, meta, fv_items, has_s3, sec_recent_status, recent_earnings,
                precomputed_wires=wires,
            ))
        except (RuntimeError, tk.TclError):
            pass

    def manual_refresh(self):
        """User-pressed refresh: pull RSS feeds + Finviz + SEC now and
        update the 'Last Refreshed' timestamp. Non-blocking — the
        network work runs on a daemon thread."""
        # Refresh exits historical mode by design (user spec).
        if self.historical_active:
            self.exit_historical_mode()
        # Button briefly disabled + marked as in-flight so a user
        # hammering the button doesn't queue up redundant fetches.
        self.btn_refresh.config(state="disabled", text="…")
        with self._gen_lock:
            self._fetch_gen += 1
            gen = self._fetch_gen
        sym = self.current_symbol
        threading.Thread(
            target=self._do_manual_refresh, args=(sym, gen), daemon=True,
        ).start()

    def _do_manual_refresh(self, sym, gen):
        # 1. One-shot RSS pull. Both the merge and the within-window
        # dedupe live inside the worker (C3 + E7), so we just call the
        # high-level helpers — no risk of clobbering the 60s loop.
        # ``wires_cached`` = the pull landed inside the worker's dedupe
        # window, so the wires were NOT re-pulled (the Finviz/SEC scrape
        # below still runs). Surfaced on the Last Refreshed label so the
        # button doesn't look like it did nothing.
        wires_cached = False
        try:
            rw = self.fetcher.rss_worker
            new_items, wires_cached = rw.fetch_feeds(report_dedupe=True)
            rw.merge_into_cache(new_items)
        except Exception as exc:
            _log.debug("manual refresh RSS pull failed: %s", type(exc).__name__)

        if gen != self._fetch_gen:
            return

        # 2. Re-scrape Finviz + SEC for the current symbol (if any).
        meta, fv_items, has_s3, sec_recent_status, recent_earnings = {}, [], False, 2, None
        if sym:
            try:
                future_meta = self.fetcher.submit(self.fetcher.scrape_finviz, sym)
                future_sec = self.fetcher.submit(
                    self.fetcher.scrape_sec_data, sym, self.current_cik,
                )
                meta, fv_items = future_meta.result()
                has_s3, sec_recent_status, recent_earnings = future_sec.result()
            except Exception as exc:
                # Broadened from a narrow tuple + logged: an unanticipated
                # scraper error must NOT kill this thread before
                # _finish_manual_refresh re-enables btn_refresh, or the
                # button stays wedged disabled.
                _log.warning("manual refresh scrape failed for %s: %s",
                             sym, type(exc).__name__)

        if gen != self._fetch_gen:
            return
        self.after(
            0,
            lambda: self._finish_manual_refresh(
                sym, meta, fv_items, has_s3, sec_recent_status, recent_earnings,
                wires_cached,
            ),
        )

    def _finish_manual_refresh(self, sym, meta, fv_items, has_s3, sec_recent_status,
                               recent_earnings, wires_cached=False):
        if sym and sym == self.current_symbol:
            self.update_full_data(sym, meta, fv_items, has_s3, sec_recent_status, recent_earnings)
        elif not sym:
            # No active symbol — just refresh the wires list from cache.
            self.current_items = self.fetcher.get_wires(sym) if sym else self.current_items
            self.refresh_ui()
        self._set_last_refreshed_now(wires_cached=wires_cached)
        self.btn_refresh.config(state="normal", text="↻")

    def _set_last_refreshed_now(self, wires_cached=False):
        if _ET_TZ is not None:
            now_et = datetime.now(_ET_TZ)
        else:
            now_et = datetime.now()
        # A refresh inside the worker's dedupe window re-scraped the
        # symbol but reused the cached wires. Say so rather than implying
        # everything on screen is freshly pulled.
        suffix = "  (wires cached)" if wires_cached else ""
        self.lbl_last_refresh.config(
            text=f"Last Refreshed: {now_et.strftime('%H:%M:%S')} ET{suffix}"
        )

    def update_full_data(self, sym, meta, fv_items, has_s3, sec_recent_status,
                         recent_earnings=None, precomputed_wires=None):
        if sym != self.current_symbol: return

        # Most-recent past 10-K/10-Q for this CIK; used by the earnings
        # resolver as the EDGAR-tier fallback and as the accession seed
        # for the async XBRL YoY backfill.
        self.current_recent_earnings = recent_earnings

        # Reuse the bg_fetch-computed wires when available (saves a
        # second 500-row regex walk on Tk thread); fall back to a
        # fresh filter when the caller didn't pre-compute (e.g.
        # _finish_manual_refresh with a stale sym).
        wires = (precomputed_wires
                  if precomputed_wires is not None
                  else self.fetcher.get_wires(sym))
        display_name = ""
        if meta.get("name"): display_name = meta["name"]
        elif self.current_window_name: display_name = self.current_window_name

        if meta.get("catalyst"):
             if display_name: display_name += f" - {meta['catalyst']}"
             else: display_name = meta['catalyst']

        self.lbl_name.config(text=display_name)

        if has_s3: self.lbl_shelf.config(text="Shelf: YES", fg=self.colors["FG"])
        else: self.lbl_shelf.config(text="Shelf: NO", fg=self.colors["CREDIT"])

        self._update_etf_label(self.current_symbol)

        if sec_recent_status == 0: self.lbl_sec_recent.config(text="SEC: <24h", fg=self.colors["SEC_HOT"])
        elif sec_recent_status == 1: self.lbl_sec_recent.config(text="SEC: <48h", fg=self.colors["SEC_WARM"])
        else: self.lbl_sec_recent.config(text="SEC: >48h", fg=self.colors["SEC_COLD"])

        seen_urls = set(); seen_headlines = set(); merged = []
        for i in wires + fv_items:
            clean_head = i['headline'].strip().lower()
            u = i.get('url')
            if u and u in seen_urls: continue
            if clean_head in seen_headlines: continue
            if u: seen_urls.add(u)
            seen_headlines.add(clean_head)
            merged.append(i)
        self.current_items = merged
        self.current_meta = meta

        # MCap (always on, large font, optional gradient) + Float
        # (toggleable, optional low/high coloration) header labels.
        self._render_mcap_label()
        self._render_float_label()

        self.refresh_meta_label()
        self.refresh_ui()
        self._set_last_refreshed_now()

    # ------------------------------------------------------------------
    # Earnings chart popup (double-click on lbl_earnings).
    # ------------------------------------------------------------------

    # Sentinel — distinguishes "parquet hasn't loaded yet" (return None
    # gracefully, the meta row stays blank) from "parquet was loaded
    # and is missing the ticker" (None too, but the caller has already
    # paid the load cost). Without this distinction, the first symbol
    # change after launch would block the Tk thread for the full
    # ``pd.read_parquet`` call (hundreds of ms on the production
    # 14k-ticker file).
    _PARQUET_NOT_LOADED = object()
    # How often to check the earnings parquet's mtime for a background
    # refresh (earnings_pipeline rewrites it while we run always-on-top).
    _PARQUET_POLL_MS = 60_000
    # Hard size cap for the user-overridable earnings parquet. The
    # production file is a few MB / ~14k tickers; 512 MB is far above any
    # legitimate size but bounds the OOM a typo'd/huge path could cause
    # (the network paths are byte-capped; this matches that posture).
    _PARQUET_MAX_BYTES = 512 * 1024 * 1024

    def _async_load_parquet(self):
        """Background-thread parquet loader. Sets
        ``_earnings_db_full_cache`` when done. Until then,
        ``_get_earnings_db_full()`` returns None and earnings-dependent
        paths gracefully no-op. When complete, marshals back onto the
        Tk thread to repaint the meta row for the currently active
        symbol (if any).

        Records the loaded file's mtime so ``_poll_parquet_freshness``
        can detect a refresh and auto-reload. A FAILED / empty read never
        clobbers an already-good cache (so a transient read error — or an
        atomic-rename window during a earnings_pipeline write — can't blank
        live data); it only sets the cache to None when nothing has been
        loaded yet (the startup / settings-change sentinel)."""
        df = None
        mtime = None
        try:
            path = self.earnings_db_path
            if path and os.path.exists(path):
                import pandas as pd
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                if size > self._PARQUET_MAX_BYTES:
                    # A typo'd / non-parquet / enormous file: refuse to
                    # parse it with full process trust (OOM guard). Keep
                    # the existing cache and log so it's diagnosable.
                    _log.warning("earnings parquet too large (%d bytes > cap "
                                 "%d); skipping load: %s",
                                 size, self._PARQUET_MAX_BYTES, path)
                else:
                    try:
                        mtime = os.path.getmtime(path)
                    except OSError:
                        mtime = None
                    loaded = pd.read_parquet(path)
                    # A malformed-but-readable parquet missing the 'ticker'
                    # column would KeyError downstream; reject it here and
                    # keep the previous cache instead.
                    if "ticker" in getattr(loaded, "columns", []):
                        df = loaded
                    else:
                        _log.warning("earnings parquet missing 'ticker' "
                                     "column; keeping previous cache: %s", path)
                        mtime = None
        except Exception as exc:
            # Distinguish a partial-write/transient read from "no file" in
            # the log (the cache-preservation behavior below is unchanged).
            _log.warning("earnings parquet load failed (%s): %s",
                         type(exc).__name__,
                         getattr(self, "earnings_db_path", "?"))
            df = None
            mtime = None
        if df is not None:
            # Single atomic assignment under the GIL — no lock needed.
            self._earnings_db_full_cache = df
            self._earnings_db_mtime = mtime
            self._earnings_tickers_cache = None  # rebuilt lazily
        elif self._earnings_db_full_cache is self._PARQUET_NOT_LOADED:
            # First attempt found no file / failed — mark loaded-but-empty
            # so _get_earnings_db_full stops returning the sentinel. (An
            # auto-reload failure leaves the existing good cache intact.)
            self._earnings_db_full_cache = None
            self._earnings_tickers_cache = None
        # Clear the reload flag ONLY AFTER the cache + mtime are installed,
        # so a concurrent poll can't observe flag=False alongside a stale
        # mtime and fire an overlapping reload (#15).
        self._parquet_reloading = False
        try:
            self.after(0, self._on_parquet_loaded)
        except (RuntimeError, tk.TclError):
            pass

    def _poll_parquet_freshness(self):
        """Periodic check: if the earnings parquet on disk has a newer
        mtime than the copy we loaded, reload it on a daemon thread so the
        always-on-top app picks up earnings_pipeline refreshes without a
        restart. The existing cache stays visible until the new DataFrame
        is ready (no blanking). Reschedules itself every poll interval."""
        try:
            path = self.earnings_db_path
            cache = getattr(self, "_earnings_db_full_cache",
                            self._PARQUET_NOT_LOADED)
            # Skip while the initial load is still in flight (sentinel) or
            # a reload is already running.
            if (path and not self._parquet_reloading
                    and cache is not self._PARQUET_NOT_LOADED
                    and os.path.exists(path)):
                try:
                    disk_mtime = os.path.getmtime(path)
                except OSError:
                    disk_mtime = None
                if disk_mtime is not None and disk_mtime != self._earnings_db_mtime:
                    self._parquet_reloading = True
                    threading.Thread(
                        target=self._async_load_parquet, daemon=True,
                        name="MS-ParquetReload",
                    ).start()
        except Exception:  # noqa: BLE001 — never let the poll loop die
            pass
        finally:
            try:
                self.after(self._PARQUET_POLL_MS, self._poll_parquet_freshness)
            except (RuntimeError, tk.TclError):
                pass

    def _on_parquet_loaded(self):
        """Called on the Tk thread when the parquet finishes loading.
        Repaints the meta row so the earnings labels populate without
        the user having to click anything."""
        if self.current_symbol:
            try:
                self.refresh_meta_label()
            except Exception:  # noqa: BLE001 — defensive
                pass

    def _get_earnings_db_full(self):
        """Non-blocking accessor for the cached parquet DataFrame.
        Returns None when the parquet hasn't finished loading yet OR
        when the file was missing / unreadable. Cache invalidates +
        re-loads when ``earnings_db_path`` changes (handled in the
        Settings dialog Save handler)."""
        cache = getattr(self, "_earnings_db_full_cache", self._PARQUET_NOT_LOADED)
        if cache is self._PARQUET_NOT_LOADED:
            return None
        return cache

    def _has_earnings_data(self, sym):
        """Quick membership test: is ``sym`` present in the configured
        earnings_history.parquet at all? Backed by the full-DB cache
        so the asterisk indicator can fire on every refresh_meta_label
        call without disk I/O after the first hit."""
        if not sym:
            return False
        sym_u = sym.upper().strip()
        cache = getattr(self, "_earnings_tickers_cache", None)
        if cache is None:
            cache = set()
            df = self._get_earnings_db_full()
            if df is not None and "ticker" in df.columns:
                cache = {t for t in df["ticker"].unique() if t}
            self._earnings_tickers_cache = cache
        return sym_u in cache

    @staticmethod
    def _fmt_short_date(ts):
        """Render a date/Timestamp as '%b %-d' on all platforms.

        Windows strftime doesn't support ``%-d`` so we manually strip
        the leading zero. Matches Finviz's earnings-date format so
        ``parse_earnings_date`` round-trips cleanly."""
        try:
            return ts.strftime("%b %d").replace(" 0", " ")
        except Exception:
            return ""

    def _resolve_earnings_display(self, sym, meta):
        """Resolve the earnings row display for ``sym``.

        Rule of thumb (Finviz-prioritized):
          * **DATE** is always Finviz's scraped earnings date (with
            BMO/AMC/AH marker preserved). Finviz scrapes on every
            symbol switch so it stays current — the prior
            earnings_dates.parquet round-trip (Nasdaq/Yahoo/Zacks/
            EDGAR/Finnhub chain) lagged or carried NaT for active
            tickers.
          * **VALUES** depend on whether the Finviz date is future
            or past:
              - **Finviz date > today** (upcoming announcement): no
                values displayed. Surprises and YoY shown on the
                Finviz page belong to the *prior reported* quarter,
                not the upcoming one — surfacing them under an
                upcoming-date anchor would misattribute.
              - **Finviz date ≤ today** (most recent reported, or
                no Finviz date): SURPRISE %s default to the live
                Finviz scrape (it often posts the beat/miss before
                the parquet refresh does), falling back to the local
                merged earnings_data parquet (finviz/zacks/finnhub/
                EDGAR, not just Zacks) only for a cell Finviz left
                blank. YoY %s are parquet-only, with async XBRL YoY
                backfill as the last resort when a hole remains.

        Returns ``None`` when no source has any displayable data.
        Otherwise:
            {
                'date_str':       'May 28 AMC',     # for the Earn: label
                'date_obj':       date,             # parsed
                'is_future':      bool,             # date > today
                'eps_surp':       '+5.34%'|None,    # display string
                'rev_surp':       '+12.10%'|None,
                'eps_yoy':        5.2|None,         # float, percent units
                'rev_yoy':        12.1|None,
                'period_ending':  date|None,        # target quarter anchor
                'in_parquet':     bool,             # drives the '*' prefix
                'needs_xbrl_yoy': bool,             # async backfill flag
                'sec_accession':  str,              # 10-K/Q accession seed
            }

        Past-row safeguard: Finnhub-proxy rows (calendar-quarter
        placeholders stamped Mar 31/Jun 30/Sep 30/Dec 31, regardless
        of the company's actual fiscal calendar) are skipped during
        past-selection. They duplicate the real Zacks/EDGAR row for
        the same quarter but carry a wrong date and NaN
        surprise_rev_pct — picking them produced the "Mar 31 with
        no Sales Sur" bug on DELL.
        """
        import pandas as pd

        if not sym or sym == "—":
            return None
        sym_u = sym.upper().strip()
        in_parquet = self._has_earnings_data(sym_u)
        today_dt = datetime.now().date()
        today_ts = pd.Timestamp(today_dt)
        STALE_CUTOFF_DAYS = 120

        # --- Finviz date + BMO/AMC marker ------------------------------
        fv_earn = (meta or {}).get("earnings", "") or ""
        fv_date = self.fetcher.parse_earnings_date(fv_earn) \
            if fv_earn.strip() else None
        m = re.search(r'\b(BMO|AMC|AH)\b', fv_earn, flags=re.IGNORECASE)
        when_marker = m.group(1).upper() if m else ""

        def _fv_date_str(d):
            s = self._fmt_short_date(pd.Timestamp(d))
            return f"{s} {when_marker}" if when_marker else s

        # --- Finviz future: date only, no values -----------------------
        if fv_date is not None and fv_date > today_dt:
            return {
                "date_str": _fv_date_str(fv_date),
                "date_obj": fv_date,
                "is_future": True,
                "eps_surp": None, "rev_surp": None,
                "eps_yoy": None, "rev_yoy": None,
                "period_ending": None,
                "in_parquet": in_parquet,
                "needs_xbrl_yoy": False,
                "sec_accession": "",
            }

        # --- Past/today (or no Finviz date): Finviz-first surprise ----
        # We only reach here when the Finviz earnings date is past/today
        # or absent (the future case returned above, date-only).
        #
        # SURPRISE %s are Finviz-first: default to the live scrape (it
        # frequently posts a beat/miss before the parquet refresh does),
        # falling back to the merged earnings_data parquet only for a
        # value Finviz didn't provide.
        #
        # YoY %s are PARQUET-ONLY (+ async XBRL backfill below). Finviz's
        # "EPS/Sales Q/Q" growth fields are not a dependable YoY source —
        # for near-breakeven EPS they diverge wildly from the reported
        # quarter-over-year math — so they are deliberately not consulted.
        fv_eps_surp = self._parse_pct_value((meta or {}).get("eps_surprise"))
        fv_rev_surp = self._parse_pct_value((meta or {}).get("sales_surprise"))

        # Most-recent past row from the merged parquet: gap-fill source
        # plus date/period fallback. Finnhub calendar-proxy placeholders
        # (wrong date, NaN surprises) are excluded from past-selection.
        df = self._get_earnings_db_full()
        parquet_row = None
        has_yoy_eps_col = False
        has_yoy_rev_col = False
        if df is not None and not df.empty:
            has_yoy_eps_col = "yoy_eps_pct" in df.columns
            has_yoy_rev_col = "yoy_rev_pct" in df.columns
            try:
                sub = df[df["ticker"] == sym_u]
            except Exception:
                sub = None
            if sub is not None and not sub.empty:
                past_mask = sub["report_date"] <= today_ts
                if "source" in sub.columns and "report_date_proxy" in sub.columns:
                    proxy_mask = (
                        (sub["source"] == "finnhub")
                        & sub["report_date_proxy"].fillna(False).astype(bool)
                    )
                    past_mask = past_mask & ~proxy_mask
                past = sub.loc[past_mask].sort_values(
                    "report_date", ascending=False,
                )
                if not past.empty:
                    rd = past.iloc[0].get("report_date")
                    if pd.notna(rd) and (today_ts - pd.Timestamp(rd)).days <= STALE_CUTOFF_DAYS:
                        parquet_row = past.iloc[0]

        def _pq_num(col, present=True):
            """Float value from the merged parquet row, or None."""
            if not present or parquet_row is None or col not in parquet_row.index:
                return None
            v = parquet_row.get(col)
            return float(v) if pd.notna(v) else None

        # Just-reported-quarter detection. The local parquet is batch-
        # refreshed, so a quarter that reported AFTER the last refresh
        # isn't here yet. When the live Finviz date is materially newer
        # than the newest parquet row we matched (or there's no matched
        # row at all), the displayed quarter is NEW: do NOT borrow that
        # older row's period_ending / sales-surprise / YoY / weak-flags
        # (the cross-quarter "hybrid row" bug). We surface the live
        # Finviz date + surprises (which DO belong to the new quarter)
        # and fill YoY asynchronously from the ty=ea page, greyed "(f)".
        NEW_QUARTER_GAP_DAYS = 50
        parquet_rd = None
        if parquet_row is not None:
            _prd = parquet_row.get("report_date")
            parquet_rd = pd.Timestamp(_prd) if pd.notna(_prd) else None
        is_new_quarter = (
            fv_date is not None
            and (parquet_rd is None
                 or (pd.Timestamp(fv_date) - parquet_rd).days > NEW_QUARTER_GAP_DAYS)
        )

        # Surprise: Finviz first, parquet fills only the gap — but for a
        # just-reported new quarter never borrow the stale row's surprise.
        if is_new_quarter:
            eps_surp_val = fv_eps_surp
            rev_surp_val = fv_rev_surp
        else:
            eps_surp_val = fv_eps_surp if fv_eps_surp is not None else _pq_num("surprise_eps_pct")
            rev_surp_val = fv_rev_surp if fv_rev_surp is not None else _pq_num("surprise_rev_pct")
        # YoY: parquet only (+ async backfill). A new quarter has no
        # parquet YoY, so leave None and let the ty=ea backfill fill it.
        if is_new_quarter:
            eps_yoy_val = None
            rev_yoy_val = None
        else:
            eps_yoy_val = _pq_num("yoy_eps_pct", has_yoy_eps_col)
            rev_yoy_val = _pq_num("yoy_rev_pct", has_yoy_rev_col)

        # Date label + period anchor: Finviz date first, then the parquet
        # report date, then the EDGAR file date. For a new quarter the
        # parquet period_ending belongs to the OLD quarter (skip it);
        # fv_date is always set when is_new_quarter so date_str holds.
        rec = getattr(self, "current_recent_earnings", None) or {}
        date_str = _fv_date_str(fv_date) if fv_date is not None else None
        date_obj = fv_date if fv_date is not None else None
        period_ending = None
        if parquet_row is not None and not is_new_quarter:
            pe = parquet_row.get("period_ending")
            period_ending = pd.Timestamp(pe).date() if pd.notna(pe) else None
            if date_str is None:
                rd = parquet_row.get("report_date")
                date_str = self._fmt_short_date(pd.Timestamp(rd))
                # Preserve the BMO/AMC/AH marker (same as the historical
                # resolver) so a Finviz-less fallback still shows the timing.
                rt = (str(parquet_row.get("report_time")) if "report_time" in parquet_row.index else "") or ""
                mm = re.search(r"\b(BMO|AMC|AH)\b", rt, flags=re.IGNORECASE)
                if mm:
                    date_str = f"{date_str} {mm.group(1).upper()}"
                date_obj = pd.Timestamp(rd).date()
        elif rec.get("file_date") and not is_new_quarter:
            period_ending = rec.get("report_date")
            if date_str is None:
                date_str = self._fmt_short_date(pd.Timestamp(rec["file_date"]))
                date_obj = rec["file_date"]

        # The whole row is keyed on a date label — nothing to anchor on
        # means nothing to show.
        if date_str is None:
            return None

        # YoY backfill arming. For a new quarter the 10-Q isn't filed yet,
        # so we use the live ty=ea page (greyed "(f)") instead of XBRL;
        # otherwise the XBRL gap-filler arms when Finviz + parquet both
        # left a YoY hole.
        if is_new_quarter:
            needs_xbrl_yoy = False
            needs_finviz_yoy = True
        else:
            needs_xbrl_yoy = (eps_yoy_val is None or rev_yoy_val is None) and bool(rec.get("accession"))
            needs_finviz_yoy = False

        # Base flags come from the matched parquet row — the WRONG quarter
        # for a new-quarter row, so suppress them (the ty=ea backfill
        # greys its own YoY via the "(f)" tag).
        if is_new_quarter:
            eps_weak = rev_weak = False
            eps_surp_weak = rev_surp_weak = False
        else:
            # Grey-out flags: YoY built on a near-zero prior-year base.
            eps_base, rev_base = self._yoy_base_values(sym_u, period_ending)
            eps_weak, rev_weak = self._yoy_weak_flags(eps_base, rev_base)
            # "(s)" flags: surprise built on a near-zero analyst estimate.
            # Base = the parquet row's estimate (Finviz and the parquet
            # agree on the adjusted estimate, so this holds even when the
            # displayed surprise % came from the live Finviz scrape).
            eps_est, rev_est = self._row_estimates(parquet_row)
            eps_surp_weak, rev_surp_weak = self._surp_weak_flags(eps_est, rev_est)

        return {
            "date_str": date_str,
            "date_obj": date_obj,
            "is_future": False,
            "eps_surp": _fmt_signed_pct(eps_surp_val),
            "rev_surp": _fmt_signed_pct(rev_surp_val),
            "eps_yoy": eps_yoy_val,
            "rev_yoy": rev_yoy_val,
            "eps_yoy_weak": eps_weak,
            "rev_yoy_weak": rev_weak,
            "eps_surp_weak": eps_surp_weak,
            "rev_surp_weak": rev_surp_weak,
            "period_ending": period_ending,
            "in_parquet": in_parquet,
            "needs_xbrl_yoy": needs_xbrl_yoy,
            "needs_finviz_yoy": needs_finviz_yoy,
            "sec_accession": rec.get("accession") or "" if needs_xbrl_yoy else "",
        }

    def _finviz_surprises_if_same_quarter(self, meta, anchor_date):
        """Return ``{'eps': '...%', 'rev': '...%'}`` when Finviz's
        earnings date is past/today AND within ±14d of ``anchor_date``.
        Else return ``{}``.

        Mirrors the chart-popup's ``date_is_past`` gate so Finviz
        surprises piped into a Zacks-anchored row always belong to the
        same reporting cycle. Without this guard we would surface the
        PRIOR quarter's surprise %s under the new quarter's date when
        Finviz is mid-cycle (its earnings field shows the upcoming
        date but its surprise cells still show the previous quarter)."""
        if not meta or anchor_date is None:
            return {}
        fv_earn = (meta.get("earnings") or "").strip()
        if not fv_earn:
            return {}
        fv_date = self.fetcher.parse_earnings_date(fv_earn)
        if fv_date is None:
            return {}
        today = datetime.now().date()
        if fv_date > today:
            return {}
        if abs((fv_date - anchor_date).days) > 14:
            return {}
        eps_str = (meta.get("eps_surprise") or "").strip()
        rev_str = (meta.get("sales_surprise") or "").strip()
        return {"eps": eps_str or None, "rev": rev_str or None}

    def _clear_earnings_labels(self):
        self.lbl_earnings.config(text="")
        self.lbl_eps_surp.config(text="")
        self.lbl_sales_surp.config(text="")
        if hasattr(self, "lbl_eps_yoy"):
            self.lbl_eps_yoy.config(text="")
            self.lbl_rev_yoy.config(text="")

    # Per-session memory budget for the enrichment caches:
    #   * XBRL companyfacts: ~1–10 MB each → 20 entries ≈ 200 MB worst case
    #   * 1-liners: tiny strings, but EDGAR full-text search can return
    #     dozens of unique accessions per lookup → 500 ample
    _XBRL_CACHE_MAX = 20
    _ONELINER_CACHE_MAX = 500

    def _xbrl_get_or_fetch(self, cik_padded, ua):
        """Thread-safe lookup + lazy fetch for the companyfacts cache.

        Returns the facts dict (with ``_accn_index`` pre-attached) or
        ``None`` on miss. Concurrent callers for the same CIK collapse
        onto a single network round-trip. LRU eviction enforces
        ``_XBRL_CACHE_MAX``."""
        if not cik_padded:
            return None
        with self._xbrl_facts_cache_lock:
            if cik_padded in self._xbrl_facts_cache:
                self._xbrl_facts_cache.move_to_end(cik_padded)
                return self._xbrl_facts_cache[cik_padded]
        # Network fetch happens OUTSIDE the lock so a slow remote can't
        # block lookups for other CIKs. We re-acquire after to commit
        # (or to merge with a peer's result if we lost the race).
        facts, _ferr = HistoricalLookup.companyfacts(cik_padded, ua)
        with self._xbrl_facts_cache_lock:
            existing = self._xbrl_facts_cache.get(cik_padded)
            if existing is not None:
                self._xbrl_facts_cache.move_to_end(cik_padded)
                return existing
            if facts:
                # Pre-build the accession index once, attached to the
                # facts dict so HistoricalLookup._facts_for_filing can
                # fast-path. Without this, each Pass-1 row in
                # _load_chart_data_with_gap_fill would re-walk the
                # entire companyfacts blob.
                try:
                    facts["_accn_index"] = _build_accn_index(facts)
                except Exception:  # noqa: BLE001 — index is an optimization
                    pass
                self._xbrl_facts_cache[cik_padded] = facts
            elif _ferr in ("not_found", "no_cik", "bad_json"):
                # Cache a negative ONLY for definitively-permanent misses
                # (foreign filer / no XBRL / malformed facts). Transient
                # failures (net/rate_limit/5xx/size_or_io) are left
                # UNcached so the next backfill attempt can retry, instead
                # of one throttle poisoning this CIK's YoY fill for the
                # whole cache lifetime.
                self._xbrl_facts_cache[cik_padded] = None
            while len(self._xbrl_facts_cache) > self._XBRL_CACHE_MAX:
                self._xbrl_facts_cache.popitem(last=False)
            return facts

    def _oneliner_get_or_fetch(self, accession, url, ua):
        """Thread-safe lookup + lazy fetch for the 1-liner cache.
        Returns ``(snippet, full)`` — empty strings cached too so a
        no-text filing isn't refetched."""
        if not accession:
            return "", ""
        with self._oneliner_cache_lock:
            if accession in self._oneliner_cache:
                self._oneliner_cache.move_to_end(accession)
                return self._oneliner_cache[accession]
        snippet, full, _err = HistoricalLookup.extract_oneliner(url, ua)
        with self._oneliner_cache_lock:
            if accession in self._oneliner_cache:
                self._oneliner_cache.move_to_end(accession)
                return self._oneliner_cache[accession]
            self._oneliner_cache[accession] = (snippet, full)
            while len(self._oneliner_cache) > self._ONELINER_CACHE_MAX:
                self._oneliner_cache.popitem(last=False)
            return (snippet, full)

    def _kickoff_main_yoy_backfill(self, sym, cik, accession):
        """Fire a daemon thread to compute YoY %s from EDGAR XBRL
        companyfacts and patch ``lbl_eps_yoy`` / ``lbl_rev_yoy`` in
        place when they're still empty.

        Reuses the per-session ``_xbrl_facts_cache`` so a repeat visit
        to the same ticker is instant after the first fetch. Bails on
        a stale ``_earnings_yoy_gen`` so a slow lookup can't overwrite
        a newer symbol's labels."""
        if not cik or not accession:
            return
        try:
            cik_padded = str(int(cik)).zfill(10)
        except (TypeError, ValueError):
            return
        ua = HEADERS.get("User-Agent", UA_LIST[0])
        gen = self._earnings_yoy_gen

        def worker():
            # Early bail BEFORE the network round-trip: if the user has
            # already switched symbols, this fetch would be discarded
            # post-hoc anyway — skip it and save the rate-limit budget.
            if gen != self._earnings_yoy_gen or sym != self.current_symbol:
                return
            try:
                facts = self._xbrl_get_or_fetch(cik_padded, ua)
                if not facts:
                    return
                yoy = HistoricalLookup.extract_yoy(facts, accession)
            except Exception as exc:
                _log.debug("XBRL YoY backfill failed for %s: %s",
                           sym, type(exc).__name__)
                return
            if gen != self._earnings_yoy_gen or sym != self.current_symbol:
                return
            try:
                self.after(0, lambda: self._apply_main_yoy_backfill(gen, sym, yoy))
            except (RuntimeError, tk.TclError):
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _apply_main_yoy_backfill(self, gen, sym, yoy):
        """Marshalled-onto-Tk-thread completion of the XBRL YoY
        backfill. Only patches labels that are still empty so a
        local-parquet value (which lands synchronously and is always
        preferred) can't be overwritten by EDGAR."""
        if gen != self._earnings_yoy_gen or sym != self.current_symbol:
            return
        if not self.var_earnings.get():
            return
        eps = yoy.get("eps_yoy") if yoy else None
        rev = yoy.get("rev_yoy") if yoy else None
        try:
            if (eps is not None and not self.lbl_eps_yoy.cget("text")):
                col = self._YOY_POS_COLOR if eps >= 0 else self._YOY_NEG_COLOR
                self.lbl_eps_yoy.config(text=f"  EPS YoY: {eps:+.1f}%", fg=col)
            if (rev is not None and not self.lbl_rev_yoy.cget("text")):
                col = self._YOY_POS_COLOR if rev >= 0 else self._YOY_NEG_COLOR
                self.lbl_rev_yoy.config(text=f"  Rev YoY: {rev:+.1f}%", fg=col)
        except tk.TclError:
            pass

    def _kickoff_main_finviz_yoy(self, sym, date_obj):
        """Daemon-thread backfill of a just-reported quarter's YoY from
        the live finviz ty=ea page, patching lbl_eps_yoy / lbl_rev_yoy in
        place (greyed + "(f)") when still empty. Used for a quarter that
        post-dates the local parquet — the parquet has no YoY for it and
        the 10-Q isn't on EDGAR yet. Generation-guarded so a slow fetch
        can't clobber a newer symbol's labels. Reuses the ty=ea cache."""
        if not sym:
            return
        gen = self._earnings_yoy_gen

        def worker():
            import pandas as pd
            # Early bail before the live finviz scrape if the user has
            # already moved on (avoids a discarded rate-limited fetch).
            if gen != self._earnings_yoy_gen or sym != self.current_symbol:
                return
            try:
                rows = self.fetcher.fetch_finviz_earnings(sym)
            except Exception as exc:
                _log.debug("finviz YoY backfill failed for %s: %s",
                           sym, type(exc).__name__)
                return
            if not rows:
                return
            # The just-reported quarter = the ty=ea row whose report_date
            # is nearest the displayed earnings date (within ~30d).
            target = pd.Timestamp(date_obj) if date_obj is not None else None
            best, best_days = None, None
            for r in rows:
                rd = r.get("report_date")
                if rd is None or pd.isna(rd):
                    continue
                if target is None:
                    if best is None or pd.Timestamp(rd) > pd.Timestamp(best["report_date"]):
                        best = r
                    continue
                d = abs((pd.Timestamp(rd) - target).days)
                if best_days is None or d < best_days:
                    best, best_days = r, d
            if best is None:
                return
            if target is not None and best_days is not None and best_days > 30:
                return

            def _num(v):
                return float(v) if (v is not None and not pd.isna(v)) else None
            eps_yoy = _num(best.get("yoy_eps_pct"))
            rev_yoy = _num(best.get("yoy_rev_pct"))
            if eps_yoy is None and rev_yoy is None:
                return
            if gen != self._earnings_yoy_gen or sym != self.current_symbol:
                return
            try:
                self.after(0, lambda: self._apply_main_finviz_yoy(
                    gen, sym, eps_yoy, rev_yoy))
            except (RuntimeError, tk.TclError):
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _apply_main_finviz_yoy(self, gen, sym, eps_yoy, rev_yoy):
        """Marshalled-onto-Tk-thread completion of the ty=ea YoY backfill.
        Greyed + "(f)" (live-finviz, not local parquet). Only patches
        labels still empty so a parquet/XBRL value isn't overwritten."""
        if gen != self._earnings_yoy_gen or sym != self.current_symbol:
            return
        if not self.var_earnings.get():
            return
        try:
            if eps_yoy is not None and not self.lbl_eps_yoy.cget("text"):
                self.lbl_eps_yoy.config(
                    text=f"  EPS YoY: {eps_yoy:+.1f}% (f)",
                    fg=self._YOY_WEAK_COLOR,
                )
            if rev_yoy is not None and not self.lbl_rev_yoy.cget("text"):
                self.lbl_rev_yoy.config(
                    text=f"  Rev YoY: {rev_yoy:+.1f}% (f)",
                    fg=self._YOY_WEAK_COLOR,
                )
        except tk.TclError:
            pass

    def _load_chart_data_with_gap_fill(self, sym, db_path):
        """Load LOCAL earnings history (the merged finviz/zacks/finnhub
        parquet) then gap-fill missing fields from EDGAR XBRL (and
        within-range missing-quarter rows from EDGAR too) and from the
        live finviz ty=ea page (quarters that post-date the parquet +
        any missing YoY). Returns ``(df, source_counts)`` where
        ``source_counts`` is ``{'local': N, 'edgar': N, 'finviz': N}``
        counting how many non-NaN value cells came from each source
        ('local' = the parquet; 'finviz' = the live ty=ea fill below +
        the most-recent past quarter's surprise fill, which happens in
        the chart-render path).

        Per-row gap fill semantics:
        - Missing ``reported_eps`` → XBRL ``YOY_EPS_TAGS`` chain
        - Missing ``reported_rev`` → XBRL ``YOY_REVENUE_TAGS`` chain
          (XBRL stores in $; parquet convention is $M, so divide)
        - Missing ``yoy_eps_pct`` / ``yoy_rev_pct`` → XBRL
          ``extract_yoy`` (computes from current vs prior-year period)
        - Surprise %s are NEVER fillable from EDGAR (local/finviz-only metric)

        Quarter matching: parquet's ``period_ending`` is day-1 of the
        last month of the fiscal quarter (e.g. 2026-03-01 for Q1 2026).
        EDGAR's ``report_date`` is the actual period end (2026-03-31).
        We match by (year, month) which handles both calendar-fiscal
        and non-calendar-fiscal companies correctly."""
        import pandas as pd
        counts = {"local": 0, "edgar": 0, "finviz": 0}
        df = self._load_earnings_history(sym, db_path)
        if df is None or df.empty:
            return df, counts

        VALUE_COLS = (
            "reported_eps", "reported_rev",
            "surprise_eps_pct", "surprise_rev_pct",
            "yoy_eps_pct", "yoy_rev_pct",
        )
        for col in VALUE_COLS:
            if col in df.columns:
                counts["local"] += int(df[col].notna().sum())

        # Ensure all the columns we want to fill actually exist so
        # ``df.at[i, col] = v`` doesn't blow up on a parquet schema
        # that's missing one of them (e.g. pre-2026-05 parquets
        # without yoy_* columns). Done up front so Pass 3 (Finviz)
        # can still run even when the EDGAR block bails on no-CIK
        # or no-companyfacts.
        for col in VALUE_COLS:
            if col not in df.columns:
                df[col] = float("nan")

        # Marker columns: True on a cell whose YoY came from the live
        # finviz (ty=ea) fill in Pass 4. They travel as df columns so
        # they survive _expand_with_gaps' reindexing; the summary table
        # reads them to grey the cell + append a "(f)" tag.
        df["_eps_yoy_fv"] = False
        df["_rev_yoy_fv"] = False

        cik = self.current_cik
        cik_padded = ""
        if cik:
            try:
                cik_padded = str(int(cik)).zfill(10)
            except (TypeError, ValueError):
                cik_padded = ""
        ua = HEADERS.get("User-Agent", UA_LIST[0])
        filings = self.fetcher.list_earnings_filings(cik) if cik_padded else []
        facts = None
        if cik_padded and filings:
            # Single companyfacts fetch per session per CIK — same
            # cache the historical-lookup enrichment and the main-row
            # YoY backfill use. The helper handles the lock, the
            # accn-index build, and LRU eviction.
            facts = self._xbrl_get_or_fetch(cik_padded, ua)

        local_periods = set()  # (year, month) for collision check
        for _, row in df.iterrows():
            pe = row.get("period_ending")
            if pe is not None and pd.notna(pe):
                pt = pd.Timestamp(pe)
                local_periods.add((pt.year, pt.month))

        # EDGAR Pass 1 + Pass 2 only run when we have everything they
        # need (cik + filings + facts). Pass 3 (Finviz) runs below
        # regardless so a ticker with no CIK still picks up the
        # latest-quarter surprise fill from the scanner's Finviz scrape.
        edgar_ready = bool(cik_padded and filings and facts)

        if edgar_ready:
            # --- Pass 1: fill missing fields in existing local rows ---
            for i, row in df.iterrows():
                pe = row.get("period_ending")
                if pe is None or pd.isna(pe):
                    continue
                pt = pd.Timestamp(pe)
                matching = None
                for f in filings:
                    rd = f.get("report_date")
                    if rd is None:
                        continue
                    if rd.year == pt.year and rd.month == pt.month:
                        matching = f
                        break
                if matching is None:
                    continue
                accession = matching.get("accession")
                if not accession:
                    continue
                start, end = HistoricalLookup.filing_period(facts, accession)
                # Reported EPS / Rev — only filled when the XBRL filing
                # period was resolvable (start+end found). Surprise %s
                # are NOT touched here (no XBRL source).
                if start and end:
                    if pd.isna(row.get("reported_eps")):
                        v = HistoricalLookup._find_fact_by_period(
                            facts, HistoricalLookup.YOY_EPS_TAGS, start, end, 0,
                        )
                        if v is not None:
                            df.at[i, "reported_eps"] = float(v)
                            counts["edgar"] += 1
                    if pd.isna(row.get("reported_rev")):
                        v = HistoricalLookup._find_fact_by_period(
                            facts, HistoricalLookup.YOY_REVENUE_TAGS, start, end, 0,
                        )
                        if v is not None:
                            # XBRL revenue is in $; parquet convention is $M.
                            df.at[i, "reported_rev"] = float(v) / _XBRL_DOLLARS_PER_MILLION
                            counts["edgar"] += 1
                # YoY — extract_yoy does its own period discovery + prior
                # year matching with the standard ±7d tolerance.
                need_eps_yoy = pd.isna(row.get("yoy_eps_pct"))
                need_rev_yoy = pd.isna(row.get("yoy_rev_pct"))
                if need_eps_yoy or need_rev_yoy:
                    yoy = HistoricalLookup.extract_yoy(facts, accession)
                    ey = yoy.get("eps_yoy")
                    ry = yoy.get("rev_yoy")
                    if ey is not None and need_eps_yoy:
                        df.at[i, "yoy_eps_pct"] = float(ey)
                        counts["edgar"] += 1
                    if ry is not None and need_rev_yoy:
                        df.at[i, "yoy_rev_pct"] = float(ry)
                        counts["edgar"] += 1

            # --- Pass 2: add EDGAR-only quarters within local date range ---
            # Build synthetic rows for any 10-K/10-Q whose period falls
            # within the local history range but isn't covered by an
            # existing local row. Keeps the chart's quarter count honest
            # without inventing data outside the user's covered range.
            all_pe = [
                pd.Timestamp(row["period_ending"])
                for _, row in df.iterrows()
                if row.get("period_ending") is not None and pd.notna(row.get("period_ending"))
            ]
            if all_pe:
                earliest = min(all_pe)
                latest = max(all_pe)
                new_rows = []
                for f in filings:
                    rd = f.get("report_date")
                    if rd is None:
                        continue
                    # Parquet-style period_ending: day-1 of the period-end month.
                    pe_norm = pd.Timestamp(year=rd.year, month=rd.month, day=1)
                    if (pe_norm.year, pe_norm.month) in local_periods:
                        continue
                    if pe_norm < earliest or pe_norm > latest:
                        continue
                    accession = f.get("accession")
                    if not accession:
                        continue
                    start, end = HistoricalLookup.filing_period(facts, accession)
                    eps_val = rev_val = None
                    if start and end:
                        eps_val = HistoricalLookup._find_fact_by_period(
                            facts, HistoricalLookup.YOY_EPS_TAGS, start, end, 0,
                        )
                        rev_val = HistoricalLookup._find_fact_by_period(
                            facts, HistoricalLookup.YOY_REVENUE_TAGS, start, end, 0,
                        )
                    yoy = HistoricalLookup.extract_yoy(facts, accession)
                    synth = {
                        "ticker": sym.upper().strip(),
                        "period_ending": pe_norm,
                        "report_date": pd.Timestamp(f["file_date"]),
                        "reported_eps": float(eps_val) if eps_val is not None else float("nan"),
                        "reported_rev": (
                            float(rev_val) / _XBRL_DOLLARS_PER_MILLION if rev_val is not None else float("nan")
                        ),
                        "surprise_eps_pct": float("nan"),
                        "surprise_rev_pct": float("nan"),
                        "yoy_eps_pct": float(yoy["eps_yoy"]) if yoy.get("eps_yoy") is not None else float("nan"),
                        "yoy_rev_pct": float(yoy["rev_yoy"]) if yoy.get("rev_yoy") is not None else float("nan"),
                    }
                    # Only insert if XBRL gave us at least one value;
                    # an empty synthetic row would just pollute the chart.
                    edgar_cells = sum(
                        1 for k in ("reported_eps", "reported_rev", "yoy_eps_pct", "yoy_rev_pct")
                        if not pd.isna(synth.get(k, float("nan")))
                    )
                    if edgar_cells == 0:
                        continue
                    counts["edgar"] += edgar_cells
                    new_rows.append(synth)
                    local_periods.add((pe_norm.year, pe_norm.month))
                if new_rows:
                    df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
                    # Recompute _anchor for new rows + re-sort.
                    if "period_ending" in df.columns and "report_date" in df.columns:
                        df["_anchor"] = df["period_ending"].fillna(df["report_date"])
                    elif "period_ending" in df.columns:
                        df["_anchor"] = df["period_ending"]
                    df = df.sort_values("_anchor", ascending=True, na_position="last")
                    df = df.reset_index(drop=True)

        # --- Pass 3: Finviz fill for the most-recent past local row ---
        # Finviz's snapshot scrapes only the latest reported quarter's
        # surprise %s, so it can only fill the single most-recent past
        # row when the local parquet is missing them. Gated on the
        # Finviz date being past/today AND
        # within ±14d of the row's report_date so we never attach the
        # prior quarter's surprises to a different period.
        today_ts = pd.Timestamp.now().normalize()
        meta = self.current_meta or {}
        fv_earn_raw = (meta.get("earnings") or "").strip()
        fv_date = (
            self.fetcher.parse_earnings_date(fv_earn_raw)
            if fv_earn_raw else None
        )
        if fv_date is not None and fv_date <= today_ts.date():
            past_mask = df["report_date"] <= today_ts if "report_date" in df.columns else None
            if past_mask is not None and past_mask.any():
                last_past_idx = df.index[past_mask][-1]
                # df may have been re-sorted ascending; the last past
                # row by index is the most recent past report.
                rd = df.at[last_past_idx, "report_date"]
                if pd.notna(rd):
                    days_off = abs((pd.Timestamp(rd).date() - fv_date).days)
                    if days_off <= 14:
                        def _parse_pct(s):
                            t = (s or "").strip().rstrip("%").strip()
                            if not t:
                                return None
                            try:
                                return float(t)
                            except ValueError:
                                return None
                        fv_eps = _parse_pct(meta.get("eps_surprise"))
                        fv_rev = _parse_pct(meta.get("sales_surprise"))
                        if (fv_eps is not None and "surprise_eps_pct" in df.columns
                                and pd.isna(df.at[last_past_idx, "surprise_eps_pct"])):
                            df.at[last_past_idx, "surprise_eps_pct"] = fv_eps
                            counts["finviz"] += 1
                        if (fv_rev is not None and "surprise_rev_pct" in df.columns
                                and pd.isna(df.at[last_past_idx, "surprise_rev_pct"])):
                            df.at[last_past_idx, "surprise_rev_pct"] = fv_rev
                            counts["finviz"] += 1

        # --- Pass 4: live finviz (ty=ea) fill ----------------------------
        # The local parquet is refreshed in batches, so a just-reported
        # quarter that post-dates the last refresh is absent here. Pull
        # the finviz earnings page and (a) BACKFILL yoy_eps_pct /
        # yoy_rev_pct for any local row missing them, and (b) APPEND
        # synthesized rows for quarters newer than anything in the
        # parquet. Both are tagged (_eps_yoy_fv / _rev_yoy_fv) so the
        # table greys the YoY + appends "(f)". A synthesized row's
        # reported/surprise are genuine finviz actuals.
        ea_rows = self.fetcher.fetch_finviz_earnings(sym)
        if ea_rows:
            ea_by_ym = {}
            for r in ea_rows:
                pe = pd.Timestamp(r["period_ending"])
                ea_by_ym[(pe.year, pe.month)] = r

            # (a) YoY gap-fill for existing rows, matched by (year, month)
            #     of period_ending (the cross-source fiscal-quarter key).
            for i in df.index:
                pe = df.at[i, "period_ending"] if "period_ending" in df.columns else None
                if pe is None or pd.isna(pe):
                    continue
                pet = pd.Timestamp(pe)
                src = ea_by_ym.get((pet.year, pet.month))
                if src is None:
                    continue
                ey, ry = src.get("yoy_eps_pct"), src.get("yoy_rev_pct")
                if (pd.isna(df.at[i, "yoy_eps_pct"]) and ey is not None
                        and not pd.isna(ey)):
                    df.at[i, "yoy_eps_pct"] = float(ey)
                    df.at[i, "_eps_yoy_fv"] = True
                    counts["finviz"] += 1
                if (pd.isna(df.at[i, "yoy_rev_pct"]) and ry is not None
                        and not pd.isna(ry)):
                    df.at[i, "yoy_rev_pct"] = float(ry)
                    df.at[i, "_rev_yoy_fv"] = True
                    counts["finviz"] += 1

            # (b) Append quarters newer than the parquet's latest period.
            latest_pe = None
            if "period_ending" in df.columns:
                pes = df["period_ending"].dropna()
                if not pes.empty:
                    latest_pe = pd.Timestamp(pes.max())
            existing_rd = (
                [pd.Timestamp(x) for x in df["report_date"].dropna().tolist()]
                if "report_date" in df.columns else []
            )
            new_rows = []
            for r in ea_rows:
                pe = pd.Timestamp(r["period_ending"])
                if latest_pe is not None and pe <= latest_pe:
                    continue
                # Defend against double-adding a quarter an orphan proxy
                # already covers: skip if an existing row reports within
                # 45d of this finviz announcement date.
                rd = pd.Timestamp(r["report_date"])
                if any(abs((x - rd).days) <= 45 for x in existing_rd):
                    continue
                row = dict(r)
                row["_eps_yoy_fv"] = not pd.isna(r.get("yoy_eps_pct"))
                row["_rev_yoy_fv"] = not pd.isna(r.get("yoy_rev_pct"))
                for vc in ("reported_eps", "reported_rev", "surprise_eps_pct",
                           "surprise_rev_pct", "yoy_eps_pct", "yoy_rev_pct"):
                    v = r.get(vc)
                    if v is not None and not pd.isna(v):
                        counts["finviz"] += 1
                new_rows.append(row)
            if new_rows:
                df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
                if "period_ending" in df.columns and "report_date" in df.columns:
                    df["_anchor"] = df["period_ending"].fillna(df["report_date"])
                elif "period_ending" in df.columns:
                    df["_anchor"] = df["period_ending"]
                df = df.sort_values("_anchor", ascending=True, na_position="last")
                df = df.reset_index(drop=True)
        return df, counts

    def open_earnings_chart(self, event=None):
        """Pop a chart window for the current symbol's earnings history.
        Bound to ``<Double-Button-1>`` on ``lbl_earnings``.

        The per-quarter data load (``_load_chart_data_with_gap_fill``) can
        do several seconds of SEC submissions + companyfacts + finviz I/O
        on a cold cache. That runs on a daemon thread so the always-on-top
        Tk UI never freezes; the render is marshalled back to the main
        thread (Tk is not thread-safe)."""
        sym = self.current_symbol
        if not sym or sym == "—":
            return
        # Re-entrancy guard: ignore extra double-clicks while a load is in
        # flight (avoids stacking duplicate background loads).
        if getattr(self, "_chart_loading", False):
            return
        db_path = self.earnings_db_path
        # Next-earnings date for the chart: trust Finviz's scraped
        # value (and its BMO/AMC/AH time-of-day marker) rather than
        # routing through earnings_pipeline's earnings_dates.parquet.
        # That parquet reconciles Nasdaq + Yahoo + Zacks + EDGAR +
        # Finnhub but in practice lags or carries NaT for active
        # tickers (e.g. DELL on 2026-05-25 had next_earnings=NaT
        # while Finviz already had May 28 BMO). Finviz scrapes on
        # every symbol switch so it stays current. Per-quarter
        # historical data is still loaded from the parquet — see
        # _load_chart_data_with_gap_fill below. Computed here on the main
        # thread (reads self.current_meta) before going off-thread.
        import pandas as pd
        finviz_earn_str = (self.current_meta or {}).get("earnings", "") or ""
        finviz_date = self.fetcher.parse_earnings_date(finviz_earn_str) \
            if finviz_earn_str else None
        # Pass the Finviz earnings date to the chart even when it is
        # today / just-reported (not only strictly-future). The chart's
        # _expand_with_gaps draws the marker only when this date is newer
        # than the latest reported quarter, so a same-day report (e.g.
        # "Jul 20 BMO" this morning, before the surprise lands in any
        # feed) still gets a marker + pending "??" placeholder — parity
        # with the landing row, and self-healing once a feed adds the
        # quarter. Upcoming vs. just-reported wording is chosen at the
        # marker label (see _render_earnings_chart_window_impl).
        next_e = pd.Timestamp(finviz_date) if finviz_date is not None else None
        # BMO = Before Market Open, AMC = After Market Close,
        # AH = After Hours. Preserved verbatim from the raw Finviz
        # cell and surfaced next to the marker label on the chart.
        m = re.search(r'\b(BMO|AMC|AH)\b', finviz_earn_str, flags=re.IGNORECASE)
        next_e_when = m.group(1).upper() if m else ""
        # If historical mode is active, snapshot the date so the
        # chart can highlight bars whose report_date is within ±1
        # calendar day. Snapshot at chart-open time, by design.
        hist_date_for_chart = (
            self.historical_date if self.historical_active else None
        )

        self._chart_loading = True

        def worker():
            try:
                df, source_counts = self._load_chart_data_with_gap_fill(
                    sym, db_path)
            except Exception as exc:
                # The data load now lives inside this guard (it used to run
                # bare on the Tk thread, outside the render try/except).
                _log.warning("chart data load failed for %s: %s",
                             sym, type(exc).__name__)
                df, source_counts = None, None
            try:
                self.after(0, lambda: self._finish_open_earnings_chart(
                    sym, df, source_counts, next_e, next_e_when,
                    hist_date_for_chart))
            except (RuntimeError, tk.TclError):
                self._chart_loading = False

        threading.Thread(target=worker, daemon=True,
                         name="MS-ChartLoad").start()

    def _finish_open_earnings_chart(self, sym, df, source_counts, next_e,
                                    next_e_when, hist_date_for_chart):
        """Main-thread completion of open_earnings_chart: render the chart
        (or the dates-only / no-data popup) from the off-thread load."""
        self._chart_loading = False
        if df is None or getattr(df, "empty", True):
            if next_e is not None:
                self._show_dates_only_popup(sym, None, next_e)
            else:
                self._show_no_earnings_data_popup(sym)
            return
        try:
            self._render_earnings_chart_window(
                sym, df, next_earnings=next_e,
                next_earnings_when=next_e_when,
                historical_date=hist_date_for_chart,
                source_counts=source_counts,
            )
        except Exception as exc:
            # Last-resort: don't let a charting bug crash the app.
            self._show_no_earnings_data_popup(
                sym, override_msg=f"Failed to render chart for {sym}:\n{exc}"
            )

    def _show_dates_only_popup(self, sym, last_e, next_e):
        """Mini popup for tickers in earnings_dates.parquet but not in
        earnings_history.parquet (typically yfinance-only small caps)."""
        import pandas as pd
        dlg = tk.Toplevel(self)
        dlg.title(f"Earnings — {sym}")
        dlg.transient(self)
        dlg.configure(bg=self.colors["BG"])
        dlg.resizable(False, False)
        try: dlg.attributes("-topmost", True)
        except tk.TclError: pass
        std = ("Segoe UI", self.base_font_size)
        last_s = pd.Timestamp(last_e).strftime("%Y-%m-%d") if last_e is not None else "—"
        next_s = pd.Timestamp(next_e).strftime("%Y-%m-%d") if next_e is not None else "—"
        msg = (
            f"No quarterly history for {sym}.\n\n"
            f"From earnings_dates.parquet:\n"
            f"   Last earnings:  {last_s}\n"
            f"   Next earnings:  {next_s}\n\n"
            "(This ticker is in the dates store but not the per-quarter\n"
            " history store — Zacks may not cover it.)"
        )
        tk.Label(
            dlg, text=msg, bg=self.colors["BG"], fg=self.colors["FG"],
            padx=16, pady=12, justify="left", font=std,
        ).pack()
        tk.Button(
            dlg, text="OK", command=dlg.destroy,
            bg=self.colors["BTN_BG"], fg=self.colors["BTN_FG"],
            borderwidth=0, padx=14, pady=2, font=std,
        ).pack(pady=(0, 12))
        dlg.bind("<Return>", lambda e: dlg.destroy())
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    def _load_earnings_history(self, sym, db_path):
        """Read the earnings parquet, return rows for ``sym`` sorted
        oldest → newest. None only if the parquet itself is unreadable
        OR there are zero rows for the ticker.

        Maximally permissive on data quality: rows missing dates,
        estimates, or surprises are ALL kept. The renderer handles
        each missing field independently — NaN bars are skipped, NaN
        line points are skipped, dateless rows fall back to row index
        for X positioning. The only reason to return None is "this
        ticker has no rows at all in the parquet"."""
        if not db_path:
            return None
        try:
            if not os.path.exists(db_path):
                return None
            import pandas as pd
            # Reuse the full-DB cache if the caller is asking for the
            # configured path; saves a ~100ms re-read on every chart
            # popup. Custom paths still go through pd.read_parquet.
            if db_path == getattr(self, "earnings_db_path", None):
                df = self._get_earnings_db_full()
                if df is None:
                    return None
            else:
                df = pd.read_parquet(db_path)
        except Exception:
            return None
        sym_u = sym.upper().strip()
        try:
            sub = df.loc[df["ticker"] == sym_u].copy()
        except Exception:
            return None
        if sub.empty:
            return None
        # Coerce the date columns up front so a sibling-app schema drift
        # (object-dtype dates) can't make a downstream vectorized
        # comparison (e.g. report_date <= today) raise TypeError into the
        # Tk callback. Unparseable values become NaT and are handled by
        # the renderer's existing per-row NaT/NaN skips.
        for _dc in ("report_date", "period_ending"):
            if _dc in sub.columns:
                sub[_dc] = pd.to_datetime(sub[_dc], errors="coerce")
        # Keep-if-orphan finnhub-proxy filter (chart path only — the
        # landing row + historical view drop proxies their own way).
        # finnhub calendar-quarter placeholders (report_date_proxy=True,
        # stamped Mar 31/Jun 30/Sep 30/Dec 31) duplicate a real
        # finviz/zacks row for the same earnings EVENT but with a wrong
        # calendar date and no rev/YoY. Their period_ending is calendar-
        # based (and the calendar date can land EITHER side of the real
        # report for a non-calendar-fiscal company), so neither period
        # nor raw date-distance reliably pairs a proxy to its twin: an
        # ADJACENT quarter's real report can fall within 45d of a
        # calendar-quarter-end proxy. We pair on the reliable cross-source
        # key instead — same reported EPS within a generous date window —
        # and drop a proxy only when such a twin exists, keeping it when
        # it's the sole coverage for its quarter (orphan).
        if ("source" in sub.columns and "report_date_proxy" in sub.columns
                and "report_date" in sub.columns and len(sub) > 1):
            PROXY_DUP_DAYS = 45
            PROXY_EPS_TOL = 0.005   # reported EPS match tolerance ($/sh)
            has_eps = "reported_eps" in sub.columns
            proxy_mask = (
                (sub["source"] == "finnhub")
                & sub["report_date_proxy"].fillna(False).astype(bool)
            )
            if proxy_mask.any():
                real = sub.loc[~proxy_mask]
                drop_idx = []
                for idx in sub.index[proxy_mask]:
                    rd = sub.at[idx, "report_date"]
                    if pd.isna(rd):
                        continue
                    p_eps = sub.at[idx, "reported_eps"] if has_eps else None
                    twin = False
                    for ridx in real.index:
                        rrd = real.at[ridx, "report_date"]
                        if pd.isna(rrd):
                            continue
                        if abs((pd.Timestamp(rrd) - pd.Timestamp(rd)).days) > PROXY_DUP_DAYS:
                            continue
                        # When the proxy carries a reported EPS, require a
                        # same-EPS non-proxy row (the twin); a date-only
                        # match would mis-pair an adjacent quarter. A proxy
                        # with no EPS carries no chart value, so a date-only
                        # match is acceptable there (drop the empty bar).
                        if has_eps and pd.notna(p_eps):
                            r_eps = real.at[ridx, "reported_eps"]
                            if pd.isna(r_eps) or abs(float(r_eps) - float(p_eps)) > PROXY_EPS_TOL:
                                continue
                        twin = True
                        break
                    if twin:
                        drop_idx.append(idx)
                if drop_idx:
                    sub = sub.drop(index=drop_idx)
        # Unified ``_anchor`` for sort + X-axis label. Prefer
        # period_ending (business cycle anchor); fall back to
        # report_date if missing. Rows where BOTH are NaN keep
        # ``_anchor`` as NaT — they still render but use row position
        # instead of date order.
        if "period_ending" in sub.columns and "report_date" in sub.columns:
            sub["_anchor"] = sub["period_ending"].fillna(sub["report_date"])
        elif "period_ending" in sub.columns:
            sub["_anchor"] = sub["period_ending"]
        elif "report_date" in sub.columns:
            sub["_anchor"] = sub["report_date"]
        else:
            sub["_anchor"] = pd.NaT
        # Sort: dated rows first (oldest → newest), then any
        # date-less rows at the end. ``na_position="last"`` does
        # exactly that.
        sub = sub.sort_values("_anchor", ascending=True, na_position="last")
        return sub.reset_index(drop=True)

    def _show_no_earnings_data_popup(self, sym, override_msg=None):
        dlg = tk.Toplevel(self)
        dlg.title("Earnings History")
        dlg.transient(self)
        dlg.configure(bg=self.colors["BG"])
        dlg.resizable(False, False)
        try:
            dlg.attributes("-topmost", True)
        except tk.TclError:
            pass
        if override_msg:
            msg = override_msg
        else:
            msg = (
                f"No earnings history available for {sym}.\n\n"
                "Ticker may be outside the Zacks-covered universe (ETFs,\n"
                "ADRs, foreign stocks, very recent IPOs, OTC).\n\n"
                "Check the Settings ⚙ dialog for the parquet path."
            )
        std = ("Segoe UI", self.base_font_size)
        tk.Label(
            dlg, text=msg, bg=self.colors["BG"], fg=self.colors["FG"],
            padx=14, pady=12, justify="left", font=std,
        ).pack()
        tk.Button(
            dlg, text="OK", command=dlg.destroy,
            bg=self.colors["BTN_BG"], fg=self.colors["BTN_FG"],
            borderwidth=0, padx=14, pady=2, font=std,
        ).pack(pady=(0, 12))
        dlg.bind("<Return>", lambda e: dlg.destroy())
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    @staticmethod
    def _snap_to_quarter_end(ts, max_offset_days=21):
        """Snap a timestamp to the nearest calendar quarter end
        (Mar 31, Jun 30, Sep 30, Dec 31) if it falls within
        ``max_offset_days``; otherwise return it unchanged. Used when
        we estimate an upcoming period_ending from a report date —
        the report typically lands 30-45 days after the period ends,
        and snapping gives us clean 'Mar'26' / 'Jun'26' labels rather
        than oddball '04-01' anchors."""
        import pandas as pd
        if ts is None or pd.isna(ts):
            return ts
        ts = pd.Timestamp(ts)
        y = ts.year
        candidates = [
            pd.Timestamp(y - 1, 12, 31),
            pd.Timestamp(y, 3, 31),
            pd.Timestamp(y, 6, 30),
            pd.Timestamp(y, 9, 30),
            pd.Timestamp(y, 12, 31),
            pd.Timestamp(y + 1, 3, 31),
        ]
        closest = min(candidates, key=lambda c: abs((c - ts).days))
        if abs((closest - ts).days) <= max_offset_days:
            return closest
        return ts

    @staticmethod
    def _expand_with_gaps(df, next_earnings, max_quarter_gap_days=135, quarter_days=91):
        """Insert NaN-row placeholders into ``df`` for missing quarters,
        AND extend forward to the upcoming reporting period when
        ``next_earnings`` is known.

        Three sources of placeholders:
          1. Holes inside the existing history — between consecutive
             ``_anchor`` dates that are > ~135 days apart.
          2. Holes between the last historical quarter and the period
             being reported next (when next_earnings is supplied and
             it covers a period more than one cadence-step away).
          3. The upcoming period itself — yellow placeholder labeled
             with the implied period_ending (NOT the report date).
             For example, AAOI last reports Dec'25 with next_earnings
             May 7, 2026: that report covers the Mar'26 period, so
             we place the future placeholder at Mar 31, 2026 and
             label it 'Mar'26'.

        Returns ``(expanded_df, gap_idx_set, future_idx_set)``.
        Cadence: 91 days/quarter; 135-day "missing" threshold matches
        the earnings_pipeline reference implementation."""
        import pandas as pd
        if df is None or df.empty:
            return df, set(), set()

        rows = df.to_dict("records")
        out_rows = []
        gap_indices = []
        empty_template = {col: float("nan") for col in df.columns}
        MAX_TOTAL_GAPS = 12  # safety cap across both gap sources
        # Typical report-vs-period lag — used to synthesize report_date
        # for gap placeholders so chart labels stay sensible.
        REPORT_LAG_DAYS = 30

        def _make_placeholder(anchor_ts, report_ts=None):
            """Build a NaN-row placeholder. ``anchor_ts`` is the
            period_ending we're standing in; ``report_ts`` (if given)
            overrides the synthesized report_date — used for the
            future placeholder where the report date IS known
            (next_earnings)."""
            ph = dict(empty_template)
            ph["_anchor"] = anchor_ts
            if report_ts is None and anchor_ts is not None and not pd.isna(anchor_ts):
                ph["report_date"] = pd.Timestamp(anchor_ts) + pd.Timedelta(days=REPORT_LAG_DAYS)
            else:
                ph["report_date"] = report_ts
            return ph

        # ---- Source 1: gaps inside the history ----
        for i, row in enumerate(rows):
            if i == 0:
                out_rows.append(row)
                continue
            prev_a = rows[i - 1].get("_anchor")
            cur_a = row.get("_anchor")
            if prev_a is None or cur_a is None or pd.isna(prev_a) or pd.isna(cur_a):
                out_rows.append(row)
                continue
            try:
                gap_days = (pd.Timestamp(cur_a) - pd.Timestamp(prev_a)).days
            except Exception:
                gap_days = 0
            n_missing = round(gap_days / float(quarter_days)) - 1
            if n_missing > 0:
                n_missing = min(n_missing, 8)
                for k in range(n_missing):
                    anchor_ts = (
                        pd.Timestamp(prev_a)
                        + pd.Timedelta(days=quarter_days * (k + 1))
                    )
                    gap_indices.append(len(out_rows))
                    out_rows.append(_make_placeholder(anchor_ts))
            out_rows.append(row)

        # ---- Sources 2 & 3: forward extension via next_earnings ----
        future_indices = []
        if next_earnings is not None:
            try:
                ne_ts = pd.Timestamp(next_earnings)
            except Exception:
                ne_ts = None

            # Most recent report_date and period anchor in the history
            # we've built so far. report_date gates whether the
            # placeholder is drawn (we only draw if the Finviz date is
            # later than the latest reported event); the period anchor
            # is still used to walk gap-filler cadence steps forward.
            last_report_date = None
            last_anchor = None
            for r in reversed(out_rows):
                rd = r.get("report_date")
                if last_report_date is None and rd is not None and not pd.isna(rd):
                    last_report_date = pd.Timestamp(rd)
                a = r.get("_anchor")
                if last_anchor is None and a is not None and not pd.isna(a):
                    last_anchor = pd.Timestamp(a)
                if last_report_date is not None and last_anchor is not None:
                    break

            if ne_ts is not None:
                # Implied period-end for the upcoming report. Reports
                # typically land 30-45 days past the period — snap
                # (ne_ts - 30 days) to the nearest calendar quarter
                # end within a 45-day window for clean labels.
                implied_period = ScannerApp._snap_to_quarter_end(
                    ne_ts - pd.Timedelta(days=30),
                    max_offset_days=45,
                )

                # Decide whether we need a future placeholder at all.
                # Rule: Finviz's date must be strictly later than the
                # latest report_date we have in the parquet. Comparing
                # report_date (not period_ending) lets us keep the
                # Finnhub-proxy bars (whose period_ending lands ~30d
                # before the report_date) and still draw the upcoming
                # placeholder — earlier logic used period_ending and
                # suppressed the placeholder whenever Finnhub's proxy
                # was the most-recent row.
                place_future = (
                    last_report_date is None
                    or ne_ts > last_report_date
                )
                if place_future:
                    # Walk forward in cadence steps from last_anchor,
                    # inserting GAP placeholders until we reach the
                    # cadence step just before implied_period; final
                    # placeholder at implied_period is the FUTURE one
                    # — its report_date is set to next_earnings since
                    # we know that exactly.
                    if last_anchor is not None:
                        cursor = last_anchor + pd.Timedelta(days=quarter_days)
                        while (implied_period - cursor).days > (quarter_days // 2):
                            if len(gap_indices) >= MAX_TOTAL_GAPS:
                                break
                            snapped = ScannerApp._snap_to_quarter_end(
                                cursor, max_offset_days=21,
                            )
                            gap_indices.append(len(out_rows))
                            out_rows.append(_make_placeholder(snapped))
                            cursor = cursor + pd.Timedelta(days=quarter_days)
                    future_ph = _make_placeholder(implied_period, report_ts=ne_ts)
                    future_indices.append(len(out_rows))
                    out_rows.append(future_ph)

        expanded = pd.DataFrame(out_rows)
        return expanded, set(gap_indices), set(future_indices)

    def _build_chart_summary_table(self, win, df, labels, eps_rep, eps_sp, rev_rep, rev_sp,
                                    gap_idx_set=None, future_idx_set=None,
                                    historical_idx_set=None,
                                    eps_yoy=None, rev_yoy=None,
                                    on_cell_click=None, header_registry=None,
                                    parent=None):
        """Append a horizontally-scrollable per-quarter data table to
        the chart Toplevel. Cells are colored fixed green (>0) / red
        (<0); zero and NaN render in default fg / muted grey.

        ``gap_idx_set`` and ``future_idx_set`` (built by
        ``_expand_with_gaps``) mark columns where the data is missing:
        gap cells render '??' in muted grey, future cells render '??'
        in the same yellow used by the upcoming-earnings line.

        ``historical_idx_set`` marks columns whose report_date is
        within ±1 day of the active historical-mode date. Headers
        render in purple to match the chart bar outlines.

        ``on_cell_click`` (callable(col_index)) is bound to <Button-1>
        on every header + data cell to drive the click-to-highlight
        feature; ``header_registry`` (a dict) receives
        ``col_index -> (header_label, base_fg)`` so the caller can
        recolor headers yellow on selection and restore them on
        deselect. Both are optional — passing neither yields the old
        static (non-interactive) table.

        ``parent`` overrides the container the table packs into (default
        ``win``); the chart passes a persistent host frame so the table
        can be torn down + rebuilt in place when colors change. Returns
        the table's ``outer`` frame."""
        import pandas as pd
        c = self.colors
        fg = c["FG"]
        size = self.base_font_size
        std = ("Segoe UI", size)
        bold = ("Segoe UI", size, "bold")

        # Surprise rows use the user-editable surprise palette; YoY rows
        # use the YoY palette (distinct so growth %s read apart from the
        # surprise %s above). All sourced from the persisted popout
        # color overrides so the table tracks the chart exactly.
        POS_COLOR = self._chart_surp_pos
        NEG_COLOR = self._chart_surp_neg
        YOY_POS_COLOR = self._chart_yoy_pos
        YOY_NEG_COLOR = self._chart_yoy_neg
        NAN_COLOR = c["CREDIT"]
        GAP_COLOR = c["CREDIT"]
        FUTURE_COLOR = "#FFE600"
        HIST_COLOR = self._chart_hist_color  # matches chart bar outlines

        gap_idx_set = gap_idx_set or set()
        future_idx_set = future_idx_set or set()
        historical_idx_set = historical_idx_set or set()

        outer = tk.Frame(parent if parent is not None else win, bg=c["BG"])
        outer.pack(side="bottom", fill="x", padx=8, pady=(2, 6))

        # Comfortably fits the 4 data rows (2 surprise + 2 YoY) + header
        # + horizontal scrollbar without the user needing to resize.
        canvas = tk.Canvas(outer, bg=c["BG"], highlightthickness=0, height=170)
        hscroll = tk.Scrollbar(outer, orient="horizontal", command=canvas.xview)
        canvas.configure(xscrollcommand=hscroll.set)
        canvas.pack(side="top", fill="x", expand=True)
        hscroll.pack(side="bottom", fill="x")

        body = tk.Frame(canvas, bg=c["BG"])
        canvas.create_window((0, 0), window=body, anchor="nw")

        def fmt(v, kind, pos_color=POS_COLOR, neg_color=NEG_COLOR):
            if pd.isna(v):
                return "—", NAN_COLOR
            v = float(v)
            if kind == "$":
                text = f"{v:.2f}"
            elif kind == "$M":
                # Compact millions: < 1k stays as-is; >= 1k drops to one decimal.
                if abs(v) >= 1000:
                    text = f"{v:,.0f}"
                else:
                    text = f"{v:,.1f}"
            elif kind == "%":
                text = f"{v:.1f}%"
            else:
                text = f"{v:.2f}"
            if v > 0:
                color = pos_color
            elif v < 0:
                color = neg_color
            else:
                color = fg
            return text, color

        # Header row: empty corner + quarter labels.
        # Gap-column headers render in muted grey, future column in
        # yellow — keeps the data-quality story consistent.
        tk.Label(body, text="", bg=c["BG"], fg=fg, font=bold).grid(
            row=0, column=0, padx=(2, 8), pady=2, sticky="e",
        )
        for j, label in enumerate(labels):
            # Historical match wins over gap/future — when the user is
            # mid-historical-lookup, the matched column is the focal
            # point of the chart and should override everything else.
            if j in historical_idx_set:
                hdr_fg = HIST_COLOR
            elif j in gap_idx_set:
                hdr_fg = GAP_COLOR
            elif j in future_idx_set:
                hdr_fg = FUTURE_COLOR
            else:
                hdr_fg = fg
            hdr_lbl = tk.Label(body, text=label, bg=c["BG"], fg=hdr_fg, font=bold)
            hdr_lbl.grid(row=0, column=j + 1, padx=2, pady=2)
            if header_registry is not None:
                header_registry[j] = (hdr_lbl, hdr_fg)
            if on_cell_click is not None:
                hdr_lbl.bind("<Button-1>", lambda e, jj=j: on_cell_click(jj))
                hdr_lbl.configure(cursor="hand2")

        # Per-column "weak YoY" masks: a YoY % is greyed when the
        # prior-year same-quarter reported value it was divided by is
        # near zero (tiny denominator → absurd %). The prior-year base
        # is looked up within this chart's own quarters by period_ending.
        base_by_ym = {}
        if "period_ending" in df.columns:
            for _, rr in df.iterrows():
                pe = rr.get("period_ending")
                if pd.notna(pe):
                    pet = pd.Timestamp(pe)
                    base_by_ym[(pet.year, pet.month)] = (
                        rr.get("reported_eps"), rr.get("reported_rev"),
                    )

        def _weak_cols(threshold, slot):
            out = []
            for j in range(len(labels)):
                weak = False
                if "period_ending" in df.columns:
                    pe = df.iloc[j].get("period_ending")
                    if pd.notna(pe):
                        pet = pd.Timestamp(pe)
                        base = base_by_ym.get((pet.year - 1, pet.month))
                        if base is not None:
                            bv = base[0] if slot == "eps" else base[1]
                            if pd.notna(bv) and abs(float(bv)) < threshold:
                                weak = True
                out.append(weak)
            return out

        eps_yoy_weak = _weak_cols(self._YOY_SMALL_BASE_EPS, "eps")
        rev_yoy_weak = _weak_cols(self._YOY_SMALL_BASE_REV, "rev")

        # Per-column "weak surprise" masks: a surprise % is flagged when
        # the analyst estimate (its denominator) is near zero. Unlike the
        # YoY case, these keep their color and instead get an "(s)" suffix.
        def _surp_weak_cols(threshold, est_col):
            out = []
            for j in range(len(labels)):
                weak = False
                if est_col in df.columns:
                    ev = df.iloc[j].get(est_col)
                    if pd.notna(ev) and abs(float(ev)) < threshold:
                        weak = True
                out.append(weak)
            return out

        eps_surp_weak = _surp_weak_cols(self._SURP_SMALL_BASE_EPS, "estimated_eps")
        rev_surp_weak = _surp_weak_cols(self._SURP_SMALL_BASE_REV, "estimated_rev")

        # Top-to-bottom: surprise pair on top (the "did they beat?"
        # signal users glance at first), then the YoY % growth pair.
        # (The Reported EPS / Reported Rev rows were dropped 2026-06 —
        # the table now carries only the surprise and YoY % signals.)
        # Row tuples are (name, series, kind, pos_color, neg_color,
        # weak_mask, mode): mode "grey" recolors a small-base cell grey
        # (YoY); mode "s" keeps the color but appends "(s)" (surprise).
        # 8th tuple slot ``fv_col`` names the marker column whose True
        # cells (live-finviz ty=ea YoY fills) get greyed + a "(f)" tag.
        rows = [
            ("EPS Surprise %", eps_sp,  "%",  None, None, eps_surp_weak, "s", None),
            ("Rev Surprise %", rev_sp,  "%",  None, None, rev_surp_weak, "s", None),
            ("EPS YoY %",      eps_yoy, "%",  YOY_POS_COLOR, YOY_NEG_COLOR, eps_yoy_weak, "grey", "_eps_yoy_fv"),
            ("Rev YoY %",      rev_yoy, "%",  YOY_POS_COLOR, YOY_NEG_COLOR, rev_yoy_weak, "grey", "_rev_yoy_fv"),
        ]
        for i, (name, series, kind, pos_c, neg_c, weak_mask, weak_mode, fv_col) in enumerate(rows):
            tk.Label(body, text=name, bg=c["BG"], fg=fg, font=std, anchor="e").grid(
                row=i + 1, column=0, padx=(2, 8), pady=1, sticky="e",
            )
            for j in range(len(labels)):
                v = series.iloc[j] if (series is not None and j < len(series)) else float("nan")
                # Per-cell NaN check: even in a gap or future column,
                # if the underlying cell HAS a value (e.g. Finviz pipe-in
                # filled a surprise % into a future placeholder), render
                # the actual value instead of '??'.
                if pd.isna(v):
                    if j in future_idx_set:
                        text, color = "??", FUTURE_COLOR
                    elif j in gap_idx_set:
                        text, color = "??", GAP_COLOR
                    else:
                        text, color = fmt(v, kind,
                                            pos_color=pos_c or POS_COLOR,
                                            neg_color=neg_c or NEG_COLOR)
                else:
                    text, color = fmt(v, kind,
                                        pos_color=pos_c or POS_COLOR,
                                        neg_color=neg_c or NEG_COLOR)
                    # Small-base treatment: grey the YoY cell, or tag the
                    # surprise cell with "(s)" (keeping its color).
                    if weak_mask is not None and j < len(weak_mask) and weak_mask[j]:
                        if weak_mode == "grey":
                            color = self._YOY_WEAK_COLOR
                        elif weak_mode == "s":
                            text = f"{text} (s)"
                    # Live-finviz (ty=ea) YoY fill: grey + "(f)" so it
                    # reads as "from finviz, not the local parquet".
                    # Overrides the blue/pink YoY color.
                    if fv_col is not None and fv_col in df.columns and j < len(df):
                        mval = df.iloc[j].get(fv_col)
                        if not pd.isna(mval) and bool(mval):
                            color = self._YOY_WEAK_COLOR
                            text = f"{text} (f)"
                cell = tk.Label(
                    body, text=text, bg=c["BG"], fg=color, font=std,
                    width=9, anchor="e",
                )
                cell.grid(row=i + 1, column=j + 1, padx=2, pady=1)
                if on_cell_click is not None:
                    cell.bind("<Button-1>", lambda e, jj=j: on_cell_click(jj))
                    cell.configure(cursor="hand2")

        # Wire the scroll region up after Tk computes the layout.
        # Height auto-fits the 4 data rows + header + h-scrollbar.
        def _update_scroll(*_):
            try:
                canvas.configure(scrollregion=canvas.bbox("all"))
                bbox = canvas.bbox("all")
                if bbox:
                    canvas.configure(height=min(220, max(120, bbox[3] - bbox[1] + 4)))
            except tk.TclError:
                pass
        body.bind("<Configure>", _update_scroll)
        return outer

    def _render_earnings_chart_window(self, sym, df, next_earnings=None,
                                      next_earnings_when="",
                                      historical_date=None, source_counts=None):
        """Thin wrapper around the renderer that, on a mid-construction
        failure, tears down the half-built Toplevel + closes the leaked
        matplotlib Figure before re-raising — so repeated render failures
        for one bad ticker can't accumulate orphaned windows/figures over
        a long always-on session. The caller still shows its error popup."""
        self._building_chart_win = None
        self._building_chart_fig_holder = None
        try:
            self._render_earnings_chart_window_impl(
                sym, df, next_earnings=next_earnings,
                next_earnings_when=next_earnings_when,
                historical_date=historical_date, source_counts=source_counts,
            )
        except Exception:
            self._teardown_failed_chart()
            raise
        finally:
            # On success (or after teardown) drop the building refs; the
            # live window persists on its own via Tk.
            self._building_chart_win = None
            self._building_chart_fig_holder = None

    def _teardown_failed_chart(self):
        """Close a half-built chart Figure + destroy its Toplevel after a
        render exception (mirrors the window's own _on_close cleanup)."""
        holder = getattr(self, "_building_chart_fig_holder", None)
        if holder:
            f = holder.get("fig")
            if f is not None:
                try:
                    import matplotlib.pyplot as _plt
                    _plt.close(f)
                except Exception:
                    pass
        win = getattr(self, "_building_chart_win", None)
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        self._building_chart_win = None
        self._building_chart_fig_holder = None

    def _render_earnings_chart_window_impl(self, sym, df, next_earnings=None,
                                           next_earnings_when="",
                                           historical_date=None,
                                           source_counts=None):
        """Non-modal Toplevel with two stacked matplotlib subplots —
        EPS on top, Revenue on bottom. Each panel pairs YoY % growth
        bars (left axis, blue + / pink −) with surprise % bars (right
        axis, green beat / red miss) — flat sign-colored to match the
        summary table. Both percent axes use an outlier-robust
        half-range (90th-percentile based) so a few extreme quarters
        can't crush the rest; off-scale bars clip to the axis edge and
        get a small true-value label. ``next_earnings`` (from the LIVE Finviz
        scrape in open_earnings_chart — NOT the sibling
        earnings_dates.parquet, which the chart path never reads) draws
        a vertical marker line at the upcoming quarter, when available.
        Fonts auto-scale to window size; the user's +/- multiplier is
        applied on top and persists in settings."""
        import matplotlib
        matplotlib.use("TkAgg")
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import (
            FigureCanvasTkAgg, NavigationToolbar2Tk,
        )
        import numpy as np
        import pandas as pd

        c = self.colors

        win = tk.Toplevel(self)
        self._building_chart_win = win  # for _teardown_failed_chart
        win.title(f"Earnings History — {sym}")
        win.configure(bg=c["BG"])
        try:
            win.attributes("-topmost", False)
        except tk.TclError:
            pass
        # Restore the user's last chart geometry + maximized state.
        # Fall back to the original 1000x940 sizing the first time the
        # chart is opened on a fresh install (no saved geometry yet).
        saved_geo = getattr(self, "earnings_chart_geometry", "") or ""
        if saved_geo:
            try:
                win.geometry(saved_geo)
            except tk.TclError:
                win.geometry("1000x940")
        else:
            win.geometry("1000x940")
        if getattr(self, "earnings_chart_maximized", False):
            try:
                # Tk's "zoomed" state == Windows maximized. Apply after
                # an idle tick so the initial geometry settles first;
                # otherwise the "normal" geometry we'd restore-to later
                # gets clobbered by the zoom event firing immediately.
                win.after(50, lambda: win.state("zoomed"))
            except tk.TclError:
                pass

        # Persist size + maximized + (eventually) font_mult on close.
        # ``state == "zoomed"`` is Tk's name for the maximized state.
        # We unmaximize before reading geometry so the returned string
        # represents the user's preferred restore size, not the
        # full-screen size — same dance ScannerApp.on_close uses.
        def _persist_chart_state():
            try:
                is_max = (win.state() == "zoomed")
            except tk.TclError:
                is_max = False
            try:
                if is_max:
                    win.state("normal")
                    win.update_idletasks()
                self.earnings_chart_geometry = win.geometry()
            except tk.TclError:
                pass
            self.earnings_chart_maximized = is_max
        # Figure ref is published into this dict once it's created below.
        # The close handler reads it back so it can call ``plt.close``
        # explicitly — otherwise matplotlib keeps the Figure alive in
        # pyplot's internal registry until interpreter shutdown, leaking
        # ~1-10 MB per chart open. Using a dict instead of a `nonlocal`
        # so the handler can be defined before the figure exists without
        # an UnboundLocalError risk if close fires mid-construction.
        fig_holder: dict = {"fig": None}
        self._building_chart_fig_holder = fig_holder  # for _teardown_failed_chart

        def _on_close():
            _persist_chart_state()
            f = fig_holder.get("fig")
            if f is not None:
                try:
                    import matplotlib.pyplot as _plt
                    _plt.close(f)
                except Exception:
                    pass
            try: win.destroy()
            except tk.TclError: pass
        win.protocol("WM_DELETE_WINDOW", _on_close)

        # ----- Top toolbar: font +/- buttons -----
        top_bar = tk.Frame(win, bg=c["BG"])
        top_bar.pack(side="top", fill="x", padx=6, pady=(4, 0))
        tk.Label(
            top_bar, text="Chart font:", bg=c["BG"], fg=c["FG"],
        ).pack(side="left", padx=(0, 4))
        font_state = {"mult": float(self.earnings_chart_font_mult)}
        lbl_mult = tk.Label(
            top_bar, text=f"{font_state['mult']:.2f}x",
            bg=c["BG"], fg=c["FG"], width=6,
        )

        def adjust_mult(delta):
            new_v = round(max(0.5, min(2.5, font_state["mult"] + delta)), 2)
            if new_v == font_state["mult"]:
                return
            font_state["mult"] = new_v
            self.earnings_chart_font_mult = new_v  # persists via on_close
            lbl_mult.config(text=f"{new_v:.2f}x")
            apply_fonts()

        def reset_mult():
            adjust_mult(1.0 - font_state["mult"])

        tk.Button(top_bar, text="−", width=3, borderwidth=0,
                  bg=c["BTN_BG"], fg=c["BTN_FG"],
                  command=lambda: adjust_mult(-0.1)).pack(side="left")
        lbl_mult.pack(side="left")
        tk.Button(top_bar, text="+", width=3, borderwidth=0,
                  bg=c["BTN_BG"], fg=c["BTN_FG"],
                  command=lambda: adjust_mult(0.1)).pack(side="left")
        tk.Button(top_bar, text="reset", borderwidth=0, padx=8,
                  bg=c["BTN_BG"], fg=c["BTN_FG"],
                  command=reset_mult).pack(side="left", padx=(6, 0))
        # "Colors…" opens the per-popout color editor. Edits persist to
        # settings (all popouts) and apply live to this chart via
        # _recolor_chart (defined far below; resolved lazily at click).
        tk.Button(top_bar, text="Colors…", borderwidth=0, padx=8,
                  bg=c["BTN_BG"], fg=c["BTN_FG"],
                  command=lambda: self._open_chart_color_settings(
                      win, _recolor_chart)).pack(side="left", padx=(12, 0))

        # ----- Data prep -----
        # Expand the frame with NaN placeholders for missing quarters
        # (gap detection) plus an upcoming-quarter slot when
        # next_earnings is known. Finviz never pipes surprises into
        # the future placeholder under the Finviz-only next-earnings
        # selector (next_e is always > today, so any "just-released"
        # surprises on the Finviz page belong to the prior reported
        # quarter, not the upcoming one).
        df, gap_idx_set, future_idx_set = self._expand_with_gaps(
            df, next_earnings,
        )
        sc = dict(source_counts) if source_counts else {"local": 0, "edgar": 0, "finviz": 0}
        n = len(df)
        x = np.arange(n)
        width = 0.38
        # X-axis labels use the actual REPORT DATE per row — that's
        # when the company announced (or will announce). Real data
        # rows carry it directly; placeholders synthesize it (gap rows
        # = period_anchor + ~30 days, future row = next_earnings).
        # Fall back to ``_anchor`` (period end) and finally to
        # "Q{n}" if neither is available.
        labels = []
        # Track which row's label_ts is its actual report_date (vs a
        # fallback to period_ending or a synthetic Q-label). The
        # historical-match check below is intentionally specific to
        # report_date — that's the ground-truth event-date column the
        # spec asks us to match against.
        report_dates_per_row = []
        for i in range(n):
            row = df.iloc[i]
            label_ts = None
            for col in ("report_date", "_anchor", "period_ending"):
                if col in df.columns:
                    v = row.get(col)
                    if v is not None and pd.notna(v):
                        label_ts = v
                        break
            if label_ts is not None:
                labels.append(pd.Timestamp(label_ts).strftime("%b %d '%y"))
            else:
                labels.append(f"Q{i+1}")
            # Capture the row's report_date specifically (not the
            # fallback) for historical-match testing.
            rd = None
            if "report_date" in df.columns:
                vrd = row.get("report_date")
                if vrd is not None and pd.notna(vrd):
                    rd = pd.Timestamp(vrd).date()
            report_dates_per_row.append(rd)

        # Historical-mode highlighting: bars whose report_date is
        # within ±1 calendar day of the active historical date get a
        # bold purple outline; their tick labels and table headers
        # render in the same purple (self._chart_hist_color, user-
        # editable). All other bar styling stays the same.
        HIST_LINEWIDTH = 2.0
        historical_idx_set = set()
        if historical_date is not None:
            for i, rd in enumerate(report_dates_per_row):
                if rd is None:
                    continue
                if abs((rd - historical_date).days) <= 1:
                    historical_idx_set.add(i)

        # Pull series defensively — any of these may be missing in a
        # partial-data ticker. Use empty arrays in those cases so
        # matplotlib renders a clean blank panel rather than crashing.
        def col(name):
            if name in df.columns:
                return df[name]
            return pd.Series([np.nan] * n)

        eps_rep = col("reported_eps")
        rev_rep = col("reported_rev")
        eps_sp = col("surprise_eps_pct")
        rev_sp = col("surprise_rev_pct")
        # YoY % growth columns (added 2026-05 to the upstream parquet —
        # `earnings_pipeline/dist/scanner_data/earnings_history.parquet`).
        # Tickers loaded from the older `earnings_pipeline` parquet
        # don't have these columns and will silently fall through to all-NaN
        # (rendered as '—' in the table — same path as any missing field).
        eps_yoy = col("yoy_eps_pct")
        rev_yoy = col("yoy_rev_pct")

        # Bars are flat sign-colored (no adaptive gradient): YoY bars
        # blue (+) / pink (−), surprise bars green (beat) / red (miss),
        # matching the summary table below. Colors come from the shared
        # class constants so the chart and table never drift apart.
        fg = c["FG"]

        # ----- Figure + axes -----
        fig = Figure(figsize=(10.0, 7.0), dpi=100, facecolor=c["BG"])
        fig_holder["fig"] = fig  # exposed to _on_close for plt.close
        ax_eps = fig.add_subplot(211)
        ax_rev = fig.add_subplot(212, sharex=ax_eps)
        for ax in (ax_eps, ax_rev):
            ax.set_facecolor(c["BG"])
            for spine in ax.spines.values():
                spine.set_color(fg)
            ax.tick_params(colors=fg)
            ax.grid(True, axis="y", alpha=0.18, color=fg)

        # Both axes use a symmetric range around 0 so the two 0-lines
        # always align at the vertical centre of the panel. Both axes
        # are now percent (YoY on the left, surprise on the right), so
        # each is scaled by an OUTLIER-ROBUST half-range: the 90th
        # percentile of |values| (×1.15), floored at a sane minimum.
        # A handful of extreme bars (e.g. a +390% surprise off a
        # near-zero estimate, or a +258% YoY off a tiny base) therefore
        # can't crush every other bar to a sliver — they clip to the
        # axis edge and get a small true-value label instead.
        def compute_robust_half(series, min_half, pctile=90, pad=1.15):
            """Half-range for a 0-centred axis, robust to outliers:
            ``max(min_half, percentile(|values|, pctile) * pad)``.
            Using a high percentile rather than the max keeps a handful
            of extreme bars from dominating the vertical scale."""
            try:
                vals = series.dropna().abs()
                if vals.empty:
                    return float(min_half)
                p = float(np.percentile(vals.to_numpy(dtype=float), pctile))
            except (ValueError, TypeError, IndexError, KeyError) as exc:
                # Narrowed from a blanket except so a genuine dtype bug
                # (e.g. a string in a numeric column silently clipping
                # every bar to the floor) is at least logged rather than
                # masked entirely. The legitimate empty case is already
                # handled by the vals.empty branch above.
                _log.debug("compute_robust_half fell back to floor: %s",
                           type(exc).__name__)
                return float(min_half)
            return max(float(min_half), p * pad)

        def add_pct_bars(ax_pct, xs, series, cap, pos_color, neg_color):
            """Draw flat sign-colored percent bars, clipped to ±cap.
            Positive → pos_color, negative → neg_color, NaN → no bar.
            Returns (bars, clipped_series); off-scale bars are labeled
            separately by ``label_clipped_bars``."""
            clipped = series.clip(lower=-cap, upper=cap)
            colors = []
            for v in series:
                if pd.isna(v):
                    colors.append("none")
                elif float(v) > 0:
                    colors.append(pos_color)
                elif float(v) < 0:
                    colors.append(neg_color)
                else:
                    colors.append(fg)  # exact 0 — zero-height anyway
            bars = ax_pct.bar(
                xs, clipped, width, color=colors, edgecolor="none",
            )
            return bars, clipped

        # Off-scale value labels are collected here so a live color
        # change (Colors… dialog) can recolor them in place.
        clip_label_artists = []

        def label_clipped_bars(ax_pct, xs, series, clipped, cap, y_frac=0.92):
            """Annotate every bar whose true magnitude exceeds the axis
            cap with its real value, placed just inside the clamped tip
            so the outlier's size is never hidden by the clip.

            ``y_frac`` sets how far up the clamped bar the label sits.
            Adjacent bars are always opposite types (YoY vs surprise),
            so the two series pass DIFFERENT fractions — the labels then
            land at different heights and don't collide on thin bars.
            Color follows the user's value-label setting (default ""
            => the date / foreground color, which reads on the dark
            background where the old black was lost)."""
            lbl_color = self._chart_label_color or fg
            for j in range(n):
                v = series.iloc[j] if j < len(series) else float("nan")
                if pd.isna(v):
                    continue
                v = float(v)
                if abs(v) <= cap + 1e-9:
                    continue
                yv = float(clipped.iloc[j])
                t = ax_pct.text(
                    xs[j], yv * y_frac, f"{v:.0f}%",
                    color=lbl_color, fontsize=7, fontweight="bold",
                    ha="center", va="center", zorder=7,
                )
                clip_label_artists.append(t)

        def mark_small_base_surprises(ax_pct, xs, surp_series, est_col, threshold, cap):
            """Overlay a grey 's' inside each surprise bar whose analyst
            estimate (the % denominator) is near zero — the surprise is
            built on a small base. Bar color/height are left unchanged."""
            if est_col not in df.columns:
                return
            clipped = surp_series.clip(lower=-cap, upper=cap)
            for j in range(n):
                ev = df.iloc[j].get(est_col)
                sv = surp_series.iloc[j] if j < len(surp_series) else float("nan")
                if pd.isna(ev) or pd.isna(sv) or abs(float(ev)) >= threshold:
                    continue
                yv = float(clipped.iloc[j])
                ax_pct.text(
                    xs[j], yv / 2.0, "s",
                    color=self._YOY_WEAK_COLOR, fontsize=8, fontweight="bold",
                    ha="center", va="center", zorder=6,
                )

        # Per-axis outlier-robust half-ranges. Left = YoY %, right =
        # surprise %; each scaled independently so its own bulk of bars
        # fills the panel.
        eps_left_half = compute_robust_half(eps_yoy, min_half=10.0)
        rev_left_half = compute_robust_half(rev_yoy, min_half=10.0)
        eps_right_half = compute_robust_half(eps_sp, min_half=10.0)
        rev_right_half = compute_robust_half(rev_sp, min_half=10.0)

        def _outline_historical(bars):
            """Apply the bold purple outline to bars whose index is in
            ``historical_idx_set``. Safe to call with an empty set —
            it's a no-op."""
            if not historical_idx_set or bars is None:
                return
            try:
                for i in historical_idx_set:
                    if 0 <= i < len(bars):
                        bars[i].set_edgecolor(self._chart_hist_color)
                        bars[i].set_linewidth(HIST_LINEWIDTH)
            except Exception:
                pass

        # ----- EPS panel -----
        # YoY % on the LEFT of the pair (left y-axis, percent; blue +
        # / pink −). Surprise % on the RIGHT (green beat / red miss).
        # Note: pass color= directly to set_title / set_ylabel so the
        # text gets the right fg from the start. Setting ax.title.set_color
        # AFTER set_title is a no-op because set_title constructs a
        # fresh Text object — that was the "black labels" bug.
        eps_yoy_bars, eps_yoy_clip = add_pct_bars(
            ax_eps, x - width / 2, eps_yoy, eps_left_half,
            self._chart_yoy_pos, self._chart_yoy_neg,
        )
        _outline_historical(eps_yoy_bars)
        label_clipped_bars(ax_eps, x - width / 2, eps_yoy, eps_yoy_clip,
                            eps_left_half)
        ax_eps.set_ylim(-eps_left_half, eps_left_half)
        ax_eps.set_ylabel("EPS YoY (%)", color=fg)
        ax_eps_title = ax_eps.set_title(
            f"{sym} — EPS by quarter", loc="left", color=fg,
        )

        # Surprise % on the RIGHT of the pair (right y-axis, percent).
        ax_eps_pct = ax_eps.twinx()
        eps_surp_bars, eps_surp_clip = add_pct_bars(
            ax_eps_pct, x + width / 2, eps_sp, eps_right_half,
            self._chart_surp_pos, self._chart_surp_neg,
        )
        _outline_historical(eps_surp_bars)
        label_clipped_bars(ax_eps_pct, x + width / 2, eps_sp, eps_surp_clip,
                            eps_right_half, y_frac=0.70)
        mark_small_base_surprises(ax_eps_pct, x + width / 2, eps_sp,
                                   "estimated_eps",
                                   self._SURP_SMALL_BASE_EPS, eps_right_half)
        ax_eps_pct.set_ylim(-eps_right_half, eps_right_half)
        ax_eps_pct.axhline(0, color=fg, linewidth=0.6, alpha=0.4)
        ax_eps_pct.set_ylabel("EPS surprise (%)", color=fg)
        ax_eps_pct.tick_params(axis="y", colors=fg)
        for spine in ax_eps_pct.spines.values():
            spine.set_color(fg)

        # ----- Revenue panel -----
        rev_yoy_bars, rev_yoy_clip = add_pct_bars(
            ax_rev, x - width / 2, rev_yoy, rev_left_half,
            self._chart_yoy_pos, self._chart_yoy_neg,
        )
        _outline_historical(rev_yoy_bars)
        label_clipped_bars(ax_rev, x - width / 2, rev_yoy, rev_yoy_clip,
                            rev_left_half)
        ax_rev.set_ylim(-rev_left_half, rev_left_half)
        ax_rev.set_ylabel("Revenue YoY (%)", color=fg)
        ax_rev_title = ax_rev.set_title(
            f"{sym} — Revenue by quarter", loc="left", color=fg,
        )

        ax_rev_pct = ax_rev.twinx()
        rev_surp_bars, rev_surp_clip = add_pct_bars(
            ax_rev_pct, x + width / 2, rev_sp, rev_right_half,
            self._chart_surp_pos, self._chart_surp_neg,
        )
        _outline_historical(rev_surp_bars)
        label_clipped_bars(ax_rev_pct, x + width / 2, rev_sp, rev_surp_clip,
                            rev_right_half, y_frac=0.70)
        mark_small_base_surprises(ax_rev_pct, x + width / 2, rev_sp,
                                   "estimated_rev",
                                   self._SURP_SMALL_BASE_REV, rev_right_half)
        ax_rev_pct.set_ylim(-rev_right_half, rev_right_half)
        ax_rev_pct.axhline(0, color=fg, linewidth=0.6, alpha=0.4)
        ax_rev_pct.set_ylabel("Revenue surprise (%)", color=fg)
        ax_rev_pct.tick_params(axis="y", colors=fg)
        for spine in ax_rev_pct.spines.values():
            spine.set_color(fg)

        # Next-earnings vertical marker. If we appended a future
        # placeholder column, the line sits AT that column so the
        # yellow-line + yellow-?? read as a single visual unit.
        # Otherwise (no upcoming date known) we skip the line. The
        # BMO/AMC/AH time-of-day marker (when Finviz supplied one)
        # appends to the label as "next: May 28 (BMO)".
        if future_idx_set:
            try:
                future_idx = sorted(future_idx_set)[0]
                ne_label = pd.Timestamp(next_earnings).strftime("%b %d") \
                    if next_earnings is not None else ""
                if ne_label and next_earnings_when:
                    ne_label = f"{ne_label} ({next_earnings_when})"
                # "reported:" once the marked quarter has already been
                # announced (date <= today, surprise not yet in a feed);
                # "next:" for a genuinely upcoming announcement.
                _mk_prefix = (
                    "reported" if (next_earnings is not None
                                   and pd.Timestamp(next_earnings)
                                   <= pd.Timestamp.now().normalize())
                    else "next"
                )
                for ax in (ax_eps, ax_rev):
                    ax.axvline(future_idx, color="#FFE600", linewidth=1.0,
                               linestyle="--", alpha=0.85)
                if ne_label:
                    ax_eps.text(
                        future_idx, ax_eps.get_ylim()[1] * 0.96,
                        f"  {_mk_prefix}: {ne_label}", color="#FFE600",
                        fontsize=9, va="top", ha="left",
                    )
            except Exception:
                pass

        # '??' annotations on each panel for missing quarters and the
        # upcoming-earnings placeholder. Drawn at y=0 (the symmetric
        # axis centre) so they straddle the 0-line consistently. Gap
        # placeholders use neutral grey, future placeholder uses the
        # same yellow as the next-earnings line.
        # Per-PANEL check: only draw '??' on a panel if BOTH that
        # panel's YoY and surprise values are NaN — otherwise the
        # bars themselves communicate what we have (e.g. Finviz pipe-in
        # gives us surprise % even when YoY is unknown).
        gap_text_color = "#888888"
        future_text_color = "#FFE600"

        def _row_panel_empty(idx, rep_series, surp_series):
            try:
                return pd.isna(rep_series.iloc[idx]) and pd.isna(surp_series.iloc[idx])
            except Exception:
                return True

        for idx in gap_idx_set:
            if _row_panel_empty(idx, eps_yoy, eps_sp):
                ax_eps.text(idx, 0, "??", color=gap_text_color,
                            fontsize=14, ha="center", va="center", alpha=0.7)
            if _row_panel_empty(idx, rev_yoy, rev_sp):
                ax_rev.text(idx, 0, "??", color=gap_text_color,
                            fontsize=14, ha="center", va="center", alpha=0.7)
        for idx in future_idx_set:
            if _row_panel_empty(idx, eps_yoy, eps_sp):
                ax_eps.text(idx, 0, "??", color=future_text_color,
                            fontsize=14, ha="center", va="center", alpha=0.95)
            if _row_panel_empty(idx, rev_yoy, rev_sp):
                ax_rev.text(idx, 0, "??", color=future_text_color,
                            fontsize=14, ha="center", va="center", alpha=0.95)

        ax_rev.set_xticks(x)
        ax_rev.set_xticklabels(labels, rotation=45, ha="right", color=fg)
        # Angle the EPS panel's date labels too. They default to
        # horizontal and overlap badly on a 20+ quarter ticker; match
        # the Revenue panel's 45° slant. Font size + historical-match
        # color are (re)applied in apply_fonts on every resize.
        ax_eps.set_xticks(x)
        ax_eps.set_xticklabels(labels, rotation=45, ha="right", color=fg)

        # Custom legend documenting the (now fixed) color encoding:
        # YoY % on the left axis (blue + / pink −), Surprise % on the
        # right axis (green beat / red miss). Two columns keep it
        # compact. Both panels use the same scheme.
        from matplotlib.patches import Patch
        # Legend lives in a holder so a live color change can recreate it
        # (matplotlib copies handle artists, so updating the originals
        # in place won't refresh the drawn swatches). apply_fonts reads
        # legend_holder["obj"].
        legend_holder = {"obj": None}

        def _rebuild_legend():
            old = legend_holder.get("obj")
            if old is not None:
                try:
                    old.remove()
                except Exception:
                    pass
            handles = [
                Patch(facecolor=self._chart_yoy_pos, edgecolor="none", label="YoY + (left)"),
                Patch(facecolor=self._chart_yoy_neg, edgecolor="none", label="YoY − (left)"),
                Patch(facecolor=self._chart_surp_pos, edgecolor="none", label="Surprise + (right)"),
                Patch(facecolor=self._chart_surp_neg, edgecolor="none", label="Surprise − (right)"),
            ]
            legend_holder["obj"] = ax_eps.legend(
                handles=handles, loc="upper left", ncol=2,
                frameon=False, labelcolor=fg, fontsize=8,
            )
        _rebuild_legend()

        latest_update = df["updated_at"].max() if "updated_at" in df.columns else pd.NaT
        sources = (
            ", ".join(sorted(df["source"].dropna().unique()))
            if "source" in df.columns else ""
        )
        as_of = (
            pd.Timestamp(latest_update).strftime("%Y-%m-%d %H:%M")
            if pd.notna(latest_update) else "—"
        )
        suptitle = f"{sym}    n={n} quarters    as of {as_of}"
        if sources:
            suptitle += f"    src: {sources}"
        sup = fig.suptitle(suptitle, color=fg, y=0.98)

        # ----- Embed canvas -----
        canvas = FigureCanvasTkAgg(fig, master=win)
        canvas.get_tk_widget().pack(side="top", fill="both", expand=True)

        # Pack toolbar BEFORE the table so visual order top-to-bottom is:
        # font controls, chart, data table, matplotlib toolbar.
        toolbar = NavigationToolbar2Tk(canvas, win)
        toolbar.update()

        # ----- Click-to-highlight a quarter (yellow) -----
        # Clicking a bar OR a table cell lights up that whole quarter's
        # column — yellow bar outlines in both panels, yellow date tick
        # label, yellow column header — mirroring the purple historical
        # highlight, just click-driven. One column at a time: clicking
        # another moves it; clicking the same one (or empty chart space)
        # clears it. Survives resizes because apply_fonts re-derives the
        # tick-label color through _xtick_color below.
        SEL_BAR_LW = 2.0
        selection = {"col": None}
        # Header widgets register here during the table build:
        # j -> (label, base_fg), so non-selected headers can be restored
        # to their original color (purple historical / grey gap / etc.).
        table_headers: dict = {}
        chart_bar_groups = [eps_yoy_bars, eps_surp_bars,
                            rev_yoy_bars, rev_surp_bars]

        def _xtick_color(i):
            """Tick-label color (also used by apply_fonts): the (editable)
            highlight color when click-selected, the (editable) historical
            color for the historical column, else fg."""
            if selection["col"] == i:
                return self._chart_sel_color
            if i in historical_idx_set:
                return self._chart_hist_color
            return fg

        def _apply_selection():
            sel = selection["col"]
            # Bars: highlight outline on the selected column; everything
            # else restored to its base (historical outline / none).
            for bars in chart_bar_groups:
                if bars is None:
                    continue
                for i in range(len(bars)):
                    if i == sel:
                        bars[i].set_edgecolor(self._chart_sel_color)
                        bars[i].set_linewidth(SEL_BAR_LW)
                    elif i in historical_idx_set:
                        bars[i].set_edgecolor(self._chart_hist_color)
                        bars[i].set_linewidth(HIST_LINEWIDTH)
                    else:
                        bars[i].set_edgecolor("none")
                        bars[i].set_linewidth(0.0)
            # Tick labels on both panels.
            for ax_lbl in (ax_eps, ax_rev):
                for i, tl in enumerate(ax_lbl.get_xticklabels()):
                    tl.set_color(_xtick_color(i))
            # Table column headers.
            for j, (lbl, base_fg) in table_headers.items():
                try:
                    lbl.config(fg=self._chart_sel_color if j == sel else base_fg)
                except tk.TclError:
                    pass
            canvas.draw_idle()

        def _select_col(j):
            if j is None or not (0 <= j < n):
                selection["col"] = None
            elif selection["col"] == j:
                selection["col"] = None   # toggle the active column off
            else:
                selection["col"] = j
            _apply_selection()

        def _on_chart_click(event):
            # Left-button only, and never while a pan/zoom tool is armed.
            if event.button != 1 or getattr(toolbar, "mode", ""):
                return
            if event.inaxes is None or event.xdata is None:
                _select_col(None)
                return
            _select_col(int(round(event.xdata)))

        canvas.mpl_connect("button_press_event", _on_chart_click)

        # ----- Source-count footer -----
        # Single muted line showing how many value cells came from each
        # source. Packed bottom-up so it lands between the chart and
        # the data table (above the table since pack(side=bottom)
        # stacks bottom-first by pack order). Empty when nothing was
        # gap-filled (e.g. cleanly local-only ticker).
        footer_parts = []
        _SOURCE_DISPLAY = {"local": "Local", "edgar": "Edgar", "finviz": "Finviz"}
        for k in ("local", "edgar", "finviz"):
            v = sc.get(k, 0)
            if v > 0:
                footer_parts.append(f"{_SOURCE_DISPLAY[k]} {v}")
        footer_text = (
            "Sources:  " + "   |   ".join(footer_parts)
            if footer_parts else ""
        )
        if footer_text:
            footer_lbl = tk.Label(
                win, text=footer_text,
                bg=c["BG"], fg=c["CREDIT"],
                font=("Segoe UI", max(7, self.base_font_size - 1)),
                anchor="w", padx=10,
            )
            footer_lbl.pack(side="bottom", fill="x", pady=(0, 2))

        # ----- Summary table (data values per quarter) -----
        # Horizontally scrollable so a 20+ quarter ticker still fits
        # cleanly. Rows top-to-bottom: EPS Surprise %, Rev Surprise %,
        # EPS YoY %, Rev YoY % (the Reported pair was dropped 2026-06).
        # Persistent host so the table can be torn down + rebuilt in
        # place when colors change (packed where the table used to sit).
        table_host = tk.Frame(win, bg=c["BG"])
        table_host.pack(side="bottom", fill="x")

        def _rebuild_table():
            for ch in table_host.winfo_children():
                ch.destroy()
            table_headers.clear()
            self._build_chart_summary_table(
                win, df, labels, eps_rep, eps_sp, rev_rep, rev_sp,
                gap_idx_set=gap_idx_set, future_idx_set=future_idx_set,
                historical_idx_set=historical_idx_set,
                eps_yoy=eps_yoy, rev_yoy=rev_yoy,
                on_cell_click=_select_col, header_registry=table_headers,
                parent=table_host,
            )
            _apply_selection()   # re-apply the active selection to fresh headers
        _rebuild_table()

        # ----- Auto font scaling -----
        # Reference window size; sizes scale with sqrt(area) to keep
        # legibility at very wide or tall windows.
        REF_W, REF_H = 1000.0, 740.0
        BASE = {
            "title": 11.0,    # subplot titles
            "axis": 9.0,      # tick labels
            "ylabel": 10.0,   # y-axis labels
            "legend": 9.0,
            "suptitle": 10.0,
            "xtick": 9.0,
        }

        def compute_factor():
            try:
                w = max(400, win.winfo_width())
                h = max(300, win.winfo_height())
            except tk.TclError:
                w, h = REF_W, REF_H
            scale = ((w * h) / (REF_W * REF_H)) ** 0.5
            scale = max(0.55, min(2.2, scale))
            return scale * font_state["mult"]

        def apply_fonts():
            f = compute_factor()
            for t in (ax_eps_title, ax_rev_title):
                t.set_fontsize(BASE["title"] * f)
            for ax in (ax_eps, ax_rev, ax_eps_pct, ax_rev_pct):
                ax.tick_params(labelsize=BASE["axis"] * f)
                ax.yaxis.label.set_fontsize(BASE["ylabel"] * f)
            # Recolor x-tick labels every redraw — set_xticklabels with
            # rotation must be reapplied after a font change to keep
            # rotation+color consistent across resizes. Color comes from
            # _xtick_color: yellow for a click-selected column, purple
            # for the active historical date, else fg.
            for ax_lbl in (ax_eps, ax_rev):
                for i, tl in enumerate(ax_lbl.get_xticklabels()):
                    tl.set_fontsize(BASE["xtick"] * f)
                    tl.set_color(_xtick_color(i))
            _lg = legend_holder.get("obj")
            if _lg is not None:
                for txt in _lg.get_texts():
                    txt.set_fontsize(BASE["legend"] * f)
                    txt.set_color(fg)
            sup.set_fontsize(BASE["suptitle"] * f)
            try:
                fig.tight_layout(rect=(0, 0, 1, 0.96))
            except Exception:
                pass
            canvas.draw_idle()

        # ----- Live recolor (driven by the Colors… dialog) -----
        # Bars grouped with their series + palette kind so a recolor can
        # re-derive each bar's fill from the current color settings.
        bar_recolor_specs = [
            (eps_yoy_bars, eps_yoy, "yoy"),
            (eps_surp_bars, eps_sp, "surp"),
            (rev_yoy_bars, rev_yoy, "yoy"),
            (rev_surp_bars, rev_sp, "surp"),
        ]

        def _recolor_chart():
            """Re-read the popout color settings and apply them live to
            THIS open chart: bar fills, value-label color, legend,
            click/historical highlights, and the data table."""
            for bars, series, kind in bar_recolor_specs:
                if bars is None:
                    continue
                pos = self._chart_yoy_pos if kind == "yoy" else self._chart_surp_pos
                neg = self._chart_yoy_neg if kind == "yoy" else self._chart_surp_neg
                for i in range(len(bars)):
                    v = series.iloc[i] if i < len(series) else float("nan")
                    if pd.isna(v):
                        continue
                    v = float(v)
                    bars[i].set_facecolor(pos if v > 0 else neg if v < 0 else fg)
            lbl_color = self._chart_label_color or fg
            for t in clip_label_artists:
                try:
                    t.set_color(lbl_color)
                except Exception:
                    pass
            _rebuild_legend()
            _rebuild_table()    # rebuild table + re-apply selection everywhere
            apply_fonts()       # resize legend text + tight_layout + redraw

        # Resize handler — only fire on the chart Toplevel itself, and
        # debounce because Tk fires <Configure> for every pixel of a
        # drag. Coalesce into a single redraw 80 ms after the last event.
        resize_after = {"id": None}

        def on_configure(event):
            if event.widget is not win:
                return
            if resize_after["id"] is not None:
                try: win.after_cancel(resize_after["id"])
                except tk.TclError: pass
            resize_after["id"] = win.after(80, apply_fonts)

        win.bind("<Configure>", on_configure)
        # Initial layout pass once Tk has computed the real geometry.
        win.after(50, apply_fonts)
        canvas.draw()

        win.bind("<Escape>", lambda e: _on_close())

    def _open_chart_color_settings(self, parent_win, recolor_cb):
        """Editor for the earnings-chart popup colors. Each edit commits
        immediately: stored as an instance attr, persisted to settings
        (so it's the new default for ALL popouts + survives restarts),
        and applied live to the open chart via ``recolor_cb``. Per-color
        "Reset" + a global "Reset all" restore the defaults. The value-
        label color may be blank, meaning "follow the date/fg color"."""
        import tkinter.colorchooser as colorchooser
        c = self.colors
        fg = c["FG"]
        std = ("Segoe UI", self.base_font_size)
        small = ("Segoe UI", max(7, self.base_font_size - 1))
        ent_bg = c.get("ACCENT", c["BG"])
        defaults = self._chart_color_defaults()

        # One settings window per chart; refocus an existing one.
        existing = getattr(parent_win, "_color_settings_win", None)
        if existing is not None:
            try:
                existing.deiconify(); existing.lift(); existing.focus_force()
                return
            except tk.TclError:
                pass

        win = tk.Toplevel(parent_win)
        win.title("Chart Colors")
        win.configure(bg=c["BG"])
        try:
            win.transient(parent_win)
        except tk.TclError:
            pass
        parent_win._color_settings_win = win

        def _on_close():
            try:
                parent_win._color_settings_win = None
            except (tk.TclError, AttributeError):
                pass
            try:
                win.destroy()
            except tk.TclError:
                pass
        win.protocol("WM_DELETE_WINDOW", _on_close)

        def effective(attr, value):
            # Blank value-label color renders as the date/fg color.
            if attr == "_chart_label_color" and value == "":
                return fg
            return value

        def commit(attr, value, swatch):
            setattr(self, attr, value)
            try:
                swatch.config(bg=effective(attr, value))
            except tk.TclError:
                pass
            self._save_chart_colors()       # persist (all popouts)
            try:
                recolor_cb()                # live-apply to this chart
            except Exception:
                pass

        # (attr, label, hint)
        specs = [
            ("_chart_yoy_pos",    "YoY positive",      "growth ≥ 0 bars + table"),
            ("_chart_yoy_neg",    "YoY negative",      "growth < 0 bars + table"),
            ("_chart_surp_pos",   "Surprise positive", "beat bars + table"),
            ("_chart_surp_neg",   "Surprise negative", "miss bars + table"),
            ("_chart_label_color","Value labels",      "clipped-bar overlay (blank = date color)"),
            ("_chart_sel_color",  "Click highlight",   "clicked-column color"),
            ("_chart_hist_color", "Historical match",  "historical-date color"),
        ]
        rows_state = []

        def make_row(r, attr, label, hint):
            tk.Label(win, text=label, bg=c["BG"], fg=fg, font=std, anchor="w").grid(
                row=r, column=0, sticky="w", padx=(10, 6), pady=3)
            var = tk.StringVar(value=getattr(self, attr))
            ent = tk.Entry(win, textvariable=var, width=10, font=std,
                           bg=ent_bg, fg=fg, insertbackground=fg)
            ent.grid(row=r, column=1, padx=(0, 6))
            swatch = tk.Label(win, text="   ", width=3,
                              bg=effective(attr, getattr(self, attr)))
            swatch.grid(row=r, column=2, padx=(0, 6))

            def on_var(*_):
                v = var.get().strip()
                if attr == "_chart_label_color" and v == "":
                    commit(attr, "", swatch)
                elif self._is_valid_hex_color(v):
                    commit(attr, v, swatch)
            var.trace_add("write", on_var)

            def pick():
                init = effective(attr, getattr(self, attr))
                try:
                    res = colorchooser.askcolor(color=init, parent=win, title=label)
                except tk.TclError:
                    res = None
                if res and res[1]:
                    var.set(res[1])     # fires on_var -> commit
            tk.Button(win, text="Pick…", borderwidth=0, padx=6,
                      bg=c["BTN_BG"], fg=c["BTN_FG"], command=pick).grid(
                row=r, column=3, padx=(0, 6))

            def reset():
                var.set(defaults[attr])  # fires on_var -> commit (blank ok)
            tk.Button(win, text="Reset", borderwidth=0, padx=6,
                      bg=c["BTN_BG"], fg=c["BTN_FG"], command=reset).grid(
                row=r, column=4, padx=(0, 6))

            tk.Label(win, text=hint, bg=c["BG"], fg=c["CREDIT"], font=small).grid(
                row=r, column=5, sticky="w", padx=(4, 10))
            rows_state.append((attr, var))

        for i, (attr, label, hint) in enumerate(specs):
            make_row(i, attr, label, hint)

        def reset_all():
            for attr, var in rows_state:
                var.set(defaults[attr])
        bar = tk.Frame(win, bg=c["BG"])
        bar.grid(row=len(specs), column=0, columnspan=6, sticky="we", pady=(8, 8))
        tk.Button(bar, text="Reset all to defaults", borderwidth=0, padx=10,
                  bg=c["BTN_BG"], fg=c["BTN_FG"], command=reset_all).pack(side="left", padx=10)
        tk.Button(bar, text="Close", borderwidth=0, padx=10,
                  bg=c["BTN_BG"], fg=c["BTN_FG"], command=_on_close).pack(side="right", padx=10)

        win.bind("<Escape>", lambda e: _on_close())

    @staticmethod
    def _parse_pct_value(pct_str):
        """Parse a percent display string (``+5.34%`` / ``-12.10%``)
        into a float, or None if unparseable. Tolerates a leading
        ``+`` sign and missing ``%`` suffix."""
        if not pct_str:
            return None
        try:
            v = float(str(pct_str).replace("%", "").replace("+", "").strip())
        except (ValueError, TypeError):
            return None
        # Reject 'nan'/'inf' (which float() would happily parse) so a
        # poisoned value can't propagate into a chart axis or label.
        return v if math.isfinite(v) else None

    def _render_mcap_label(self):
        """Paint the always-on MCap header label (large font). When the
        stepped gradient is enabled, color it by the symbol's USD market
        cap tier; otherwise use the theme's default fg."""
        if not hasattr(self, "lbl_mcap"):
            return
        meta = getattr(self, "current_meta", None) or {}
        mcap = meta.get("mcap")
        if not mcap:
            self.lbl_mcap.config(text="")
            return
        col = self.colors["FG"]
        if getattr(self, "mcap_gradient_enabled", True):
            tier = _mcap_tier(_parse_mcap_dollars(mcap))
            if tier:
                tier_colors = getattr(self, "mcap_tier_colors", None) or {}
                col = tier_colors.get(tier) or MCAP_TIER_DEFAULT_COLORS[tier]
        self.lbl_mcap.config(text=f"MCap {mcap}", fg=col)

    def _render_float_label(self):
        """Paint the toggleable Float header label (default font). Hidden
        unless the control-row Float checkbox is on; colored low/high only
        when Float coloration is enabled in Settings."""
        if not hasattr(self, "lbl_float"):
            return
        meta = getattr(self, "current_meta", None) or {}
        flt = meta.get("float")
        if not (self.var_float.get() and flt):
            self.lbl_float.config(text="")
            return
        if getattr(self, "float_color_enabled", True):
            if meta.get("is_low"):
                col = getattr(self, "float_low_color", "") or self.colors["TXT_OK"]
            else:
                col = getattr(self, "float_high_color", "") or self.colors["TXT_BAD"]
        else:
            col = self.colors["FG"]
        self.lbl_float.config(text=f"Float {flt}", fg=col)

    def refresh_meta_label(self):
        # Invalidate any in-flight async YoY backfill (finviz ty=ea OR
        # XBRL) armed by a PRIOR paint: this repaint may have flipped the
        # earnings row to a future / different quarter (e.g. Finviz rolled
        # its date forward after a manual refresh), and a stale worker
        # must not paint last quarter's YoY onto it (the cross-quarter
        # "hybrid row" race). A still-valid fill is re-armed below with
        # the new generation. (manual_refresh bumps only _fetch_gen, so
        # without this the YoY workers would survive a refresh.) Guarded
        # with getattr because refresh_meta_label can run during __init__
        # before _gen_lock is created — at which point no worker is in
        # flight, so the bump is unnecessary anyway.
        _gl = getattr(self, "_gen_lock", None)
        if _gl is not None:
            with _gl:
                self._earnings_yoy_gen += 1
        m_txt = []
        meta = self.current_meta
        if meta.get("short"): m_txt.append(f"Short {meta['short']}")
        if self.var_rvol.get() and meta.get("rvol"): m_txt.append(f"RVol {meta['rvol']}")
        if meta.get("sector"): m_txt.append(f"{meta['sector']}")
        if meta.get("country"): m_txt.append(f"{meta['country']}")
        self.lbl_meta.config(text="  |  ".join(m_txt))

        if not self.var_earnings.get():
            self._clear_earnings_labels()
            return

        # Earnings row. LIVE mode resolves from the Finviz scrape +
        # parquet (surprise = Finviz-first, YoY = parquet). HISTORICAL
        # mode resolves purely from the parquet for the looked-up date
        # window. Both paint the SAME top labels via _paint_earnings_row
        # so the Historical view is identical to the landing page.
        if self.historical_active and self.historical_date is not None:
            try:
                resolved = self._resolve_historical_earnings_display(
                    self.current_symbol, self.historical_date,
                )
            except Exception as exc:
                # A malformed/object-dtype parquet datetime (sibling
                # schema drift) must clear the labels (the documented
                # "returns None -> labels clear" contract), not raise a
                # traceback into this Tk refresh handler.
                _log.debug("historical earnings resolve failed: %s",
                           type(exc).__name__)
                resolved = None
        else:
            resolved = self._resolve_earnings_display(self.current_symbol, meta)
        if resolved is None:
            self._clear_earnings_labels()
            return
        self._paint_earnings_row(resolved)

    def _paint_earnings_row(self, resolved):
        """Paint the top earnings labels from a resolved dict. Shared by
        the live ``refresh_meta_label`` and the Historical view; the dict
        follows the ``_resolve_earnings_display`` contract."""
        star = "*" if resolved["in_parquet"] else ""
        earn_str = resolved["date_str"]
        prox = self.fetcher.earnings_proximity(
            earn_str,
            past_days=self.earn_past_days,
            future_days=self.earn_future_days,
        )

        # Date label is the ONLY label that picks up the user's
        # settings-menu colors (earn_future_color / earn_pos_color /
        # earn_neg_color). The pos/neg variants apply when the event
        # is inside the past-window AND we know the EPS surprise sign
        # (a beat/miss visual hint for the most recent report).
        # Surprises + YoY both use fixed sign-keyed constants below
        # and ignore proximity entirely.
        eps_surp = resolved.get("eps_surp")
        rev_surp = resolved.get("rev_surp")
        eps_surp_val = self._parse_pct_value(eps_surp)
        if prox == "future":
            date_color = self.earn_future_color
        elif prox == "past" and eps_surp_val is not None and eps_surp_val > 0:
            date_color = self.earn_pos_color
        elif prox == "past" and eps_surp_val is not None and eps_surp_val < 0:
            date_color = self.earn_neg_color
        else:
            date_color = self.colors["FG"]
        self.lbl_earnings.config(text=f"{star}Earn: {earn_str}", fg=date_color)

        # Future safeguard: hide all numeric fields when the displayed
        # event is upcoming. Per the spec, we never backfill with
        # last-quarter values for a future-anchored row.
        if resolved["is_future"]:
            self.lbl_eps_surp.config(text="")
            self.lbl_sales_surp.config(text="")
            self.lbl_eps_yoy.config(text="")
            self.lbl_rev_yoy.config(text="")
            return

        # Surprise %s — always green/positive / red/negative regardless
        # of proximity (parallel to YoY's always-blue/pink treatment).
        # Uses fixed constants, not the user-settings colors; those are
        # reserved for the date label.
        def _surp_color(s):
            v = self._parse_pct_value(s)
            if v is None:
                return self.colors["FG"]
            if v > 0:
                return self._SURP_POS_COLOR
            if v < 0:
                return self._SURP_NEG_COLOR
            return self.colors["FG"]

        # An "(s)" marker flags a surprise built on a near-zero estimate
        # (small-base) — the value keeps its green/red color.
        eps_s = " (s)" if resolved.get("eps_surp_weak") else ""
        rev_s = " (s)" if resolved.get("rev_surp_weak") else ""
        if eps_surp:
            self.lbl_eps_surp.config(
                text=f"  EPS Sur: {eps_surp}{eps_s}", fg=_surp_color(eps_surp),
            )
        else:
            self.lbl_eps_surp.config(text="")
        if rev_surp:
            self.lbl_sales_surp.config(
                text=f"  Sales Sur: {rev_surp}{rev_s}", fg=_surp_color(rev_surp),
            )
        else:
            self.lbl_sales_surp.config(text="")

        # YoY %s — blue (positive) / pink (negative). Source order is
        # merged parquet → EDGAR (the last filled asynchronously via the
        # backfill below); Finviz is never a YoY source.
        eps_yoy = resolved.get("eps_yoy")
        rev_yoy = resolved.get("rev_yoy")
        if eps_yoy is not None:
            if resolved.get("eps_yoy_weak"):
                col = self._YOY_WEAK_COLOR
            else:
                col = self._YOY_POS_COLOR if eps_yoy >= 0 else self._YOY_NEG_COLOR
            self.lbl_eps_yoy.config(text=f"  EPS YoY: {eps_yoy:+.1f}%", fg=col)
        else:
            self.lbl_eps_yoy.config(text="")
        if rev_yoy is not None:
            if resolved.get("rev_yoy_weak"):
                col = self._YOY_WEAK_COLOR
            else:
                col = self._YOY_POS_COLOR if rev_yoy >= 0 else self._YOY_NEG_COLOR
            self.lbl_rev_yoy.config(text=f"  Rev YoY: {rev_yoy:+.1f}%", fg=col)
        else:
            self.lbl_rev_yoy.config(text="")

        # Async XBRL YoY backfill — only fires when we still have a
        # YoY gap AND we have a 10-K/Q accession to work from. The
        # backfill respects label state (won't overwrite local-filled
        # cells) and the generation counter (won't touch stale tickers).
        if resolved.get("needs_xbrl_yoy") and resolved.get("sec_accession"):
            self._kickoff_main_yoy_backfill(
                self.current_symbol, self.current_cik,
                resolved["sec_accession"],
            )

        # Async live-finviz (ty=ea) YoY backfill — fires for a
        # just-reported quarter that post-dates the local parquet, where
        # the parquet has no YoY and the 10-Q isn't on EDGAR yet. Fills
        # lbl_eps_yoy / lbl_rev_yoy greyed + "(f)" when still empty.
        if resolved.get("needs_finviz_yoy"):
            self._kickoff_main_finviz_yoy(
                self.current_symbol, resolved.get("date_obj"),
            )

    def _resolve_historical_earnings_display(self, sym, target_date,
                                             days_before=2, days_after=5):
        """Parquet-only earnings resolution for the Historical view.

        Finds the merged earnings_data row whose ``report_date`` falls in
        ``[target-2, target+5]`` (the same window the historical EDGAR
        search uses) and returns the ``_resolve_earnings_display`` dict
        shape so the top labels paint identically to the landing page.
        Surprise %s AND YoY %s both come from the parquet. Returns
        ``None`` when no row lands in the window (labels then clear) —
        there is no live fallback by design.

        Finnhub calendar-proxy placeholders (wrong date, NaN surprises)
        are excluded. When >1 report lands in the window, the one nearest
        the entered date wins."""
        import pandas as pd
        if not sym or sym == "—" or target_date is None:
            return None
        df = self._get_earnings_db_full()
        if df is None or getattr(df, "empty", True):
            return None
        sym_u = sym.upper().strip()
        try:
            sub = df[df["ticker"] == sym_u]
        except Exception:
            return None
        if sub.empty:
            return None
        tgt = pd.Timestamp(target_date)
        lo = tgt - pd.Timedelta(days=days_before)
        hi = tgt + pd.Timedelta(days=days_after)
        win = sub[(sub["report_date"] >= lo) & (sub["report_date"] <= hi)]
        if "source" in win.columns and "report_date_proxy" in win.columns:
            win = win[~((win["source"] == "finnhub")
                        & win["report_date_proxy"].fillna(False).astype(bool))]
        if win.empty:
            return None
        # Nearest report_date to the entered date wins.
        order = (win["report_date"] - tgt).abs().sort_values().index
        row = win.loc[order[0]]
        rd = row["report_date"]

        has_yoy_eps = "yoy_eps_pct" in df.columns
        has_yoy_rev = "yoy_rev_pct" in df.columns

        def _num(col, ok=True):
            if not ok or col not in row.index:
                return None
            v = row.get(col)
            return float(v) if pd.notna(v) else None

        eps_surp_val = _num("surprise_eps_pct")
        rev_surp_val = _num("surprise_rev_pct")
        eps_yoy_val = _num("yoy_eps_pct", has_yoy_eps)
        rev_yoy_val = _num("yoy_rev_pct", has_yoy_rev)

        # Earn-date label = parquet report_date + any BMO/AMC/AH marker.
        date_str = self._fmt_short_date(pd.Timestamp(rd))
        rt = (str(row.get("report_time")) if "report_time" in row.index else "") or ""
        m = re.search(r"\b(BMO|AMC|AH)\b", rt, flags=re.IGNORECASE)
        if m:
            date_str = f"{date_str} {m.group(1).upper()}"

        pe = row.get("period_ending") if "period_ending" in row.index else None
        eps_base, rev_base = self._yoy_base_values(sym_u, pe)
        eps_weak, rev_weak = self._yoy_weak_flags(eps_base, rev_base)
        eps_est, rev_est = self._row_estimates(row)
        eps_surp_weak, rev_surp_weak = self._surp_weak_flags(eps_est, rev_est)
        return {
            "date_str": date_str,
            "date_obj": pd.Timestamp(rd).date(),
            "is_future": False,
            "eps_surp": _fmt_signed_pct(eps_surp_val),
            "rev_surp": _fmt_signed_pct(rev_surp_val),
            "eps_yoy": eps_yoy_val,
            "rev_yoy": rev_yoy_val,
            "eps_yoy_weak": eps_weak,
            "rev_yoy_weak": rev_weak,
            "eps_surp_weak": eps_surp_weak,
            "rev_surp_weak": rev_surp_weak,
            "period_ending": (pd.Timestamp(pe).date() if pd.notna(pe) else None),
            "in_parquet": True,
            "needs_xbrl_yoy": False,
            "sec_accession": "",
        }

    def _yoy_base_values(self, sym, period_ending):
        """Prior-year same-quarter reported (eps, rev) from the parquet —
        the denominators a YoY % was divided by. Used to grey out YoY
        figures built on a near-zero base. ``(None, None)`` when the
        prior-year row is unavailable (then the YoY keeps its normal
        blue/pink — we don't grey what we can't assess)."""
        import pandas as pd
        if not sym or period_ending is None:
            return (None, None)
        df = self._get_earnings_db_full()
        if df is None or getattr(df, "empty", True):
            return (None, None)
        try:
            pe = pd.Timestamp(period_ending)
            # Parquet period_ending is day-1 of the quarter's last month;
            # the prior-year quarter is the same month, one year back.
            prior = pd.Timestamp(year=pe.year - 1, month=pe.month, day=1)
            sub = df[(df["ticker"] == sym.upper().strip())
                     & (df["period_ending"] == prior)]
            if sub.empty:
                return (None, None)
            r = sub.iloc[0]

            def g(c):
                v = r.get(c) if c in r.index else None
                return float(v) if pd.notna(v) else None

            return (g("reported_eps"), g("reported_rev"))
        except Exception:
            return (None, None)

    @classmethod
    def _yoy_weak_flags(cls, eps_base, rev_base):
        """(eps_weak, rev_weak) booleans: True when the prior-year base
        magnitude is below the small-base thresholds (YoY unreliable)."""
        eps_weak = eps_base is not None and abs(eps_base) < cls._YOY_SMALL_BASE_EPS
        rev_weak = rev_base is not None and abs(rev_base) < cls._YOY_SMALL_BASE_REV
        return eps_weak, rev_weak

    @staticmethod
    def _row_estimates(row):
        """(eps_estimate, rev_estimate) from a parquet row, or (None,
        None) — the denominators a SURPRISE % was divided by."""
        import pandas as pd
        if row is None:
            return (None, None)

        def g(c):
            try:
                v = row.get(c) if c in row.index else None
            except Exception:
                v = None
            return float(v) if v is not None and pd.notna(v) else None

        return (g("estimated_eps"), g("estimated_rev"))

    @classmethod
    def _surp_weak_flags(cls, eps_est, rev_est):
        """(eps_weak, rev_weak) booleans: True when the analyst estimate
        magnitude is below the small-base thresholds (surprise % built on
        a near-zero denominator)."""
        eps_weak = eps_est is not None and abs(eps_est) < cls._SURP_SMALL_BASE_EPS
        rev_weak = rev_est is not None and abs(rev_est) < cls._SURP_SMALL_BASE_REV
        return eps_weak, rev_weak

    def _parse_hot_words(self, text):
        """Parse highlight terms. Quoted terms match whole-word only; unquoted match substring."""
        terms = []
        raw = text
        for m in re.finditer(r'"([^"]+)"', raw):
            word = m.group(1).strip()
            if word:
                terms.append(("exact", re.compile(r'(?<!\w)' + re.escape(word) + r'(?!\w)', re.IGNORECASE)))
            raw = raw.replace(m.group(0), "", 1)
        for part in raw.split(","):
            w = part.strip().lower()
            if w:
                terms.append(("sub", w))
        return terms

    def apply_hot_words(self, event=None):
        self.hot_words_new = self._parse_hot_words(self.entry_hot_new.get())
        self.hot_words_old = self._parse_hot_words(self.entry_hot_old.get())
        # Hot-word changes never affect *visibility* — only tags. Skip
        # the full rebuild and just re-tag rows in place (E6).
        self._retag_visible_rows()

    # ------------------------------------------------------------------
    # Search filter — keyword + date
    # ------------------------------------------------------------------
    def _toggle_search_row(self):
        if self.search_visible.get():
            self.search_row.pack_forget()
            self.historical_row.pack_forget()
            self.search_visible.set(False)
            self.btn_search_toggle.config(text="🔍 ▸")
        else:
            # Insert immediately after the highlight row, with the
            # historical row stacked just below the keyword/date row.
            self.search_row.pack(fill="x", padx=10, pady=(0, 0),
                                  after=self.refresh_info)
            self.historical_row.pack(fill="x", padx=10, pady=(0, 0),
                                      after=self.search_row)
            self.search_visible.set(True)
            self.btn_search_toggle.config(text="🔍 ▾")
            self.entry_search_kw.focus_set()

    def apply_search(self, event=None):
        kw_text = self.entry_search_kw.get()
        date_text = self.entry_search_date.get().strip()
        # Reuse the highlight grammar for keyword search: "exact" =
        # whole-word, unquoted = substring. Multi-term = OR.
        self.search_keywords = self._parse_hot_words(kw_text) if kw_text.strip() else []
        if date_text:
            pred, err = self._parse_date_filter(date_text)
            if err:
                self.search_date_pred = None
                self._show_search_error(err)
                return
            self.search_date_pred = pred
        else:
            self.search_date_pred = None
        self._show_search_error("")
        self.refresh_ui()

    def clear_search(self):
        self.entry_search_kw.delete(0, "end")
        self.entry_search_date.delete(0, "end")
        self.search_keywords = []
        self.search_date_pred = None
        self._show_search_error("")
        self.refresh_ui()

    def _show_search_error(self, msg):
        if msg:
            self.lbl_search_err.config(text=f"  {msg}")
            if not self.lbl_search_err.winfo_ismapped():
                self.lbl_search_err.pack(side="left", padx=(8, 0))
        else:
            self.lbl_search_err.config(text="")
            if self.lbl_search_err.winfo_ismapped():
                self.lbl_search_err.pack_forget()

    # ------------------------------------------------------------------
    # Historical lookup (Polygon news + EDGAR full-text)
    # ------------------------------------------------------------------
    def _show_historical_error(self, msg):
        if msg:
            self.lbl_historical_err.config(text=f"  {msg}")
            if not self.lbl_historical_err.winfo_ismapped():
                self.lbl_historical_err.pack(side="left", padx=(8, 0))
        else:
            self.lbl_historical_err.config(text="")
            if self.lbl_historical_err.winfo_ismapped():
                self.lbl_historical_err.pack_forget()

    def _parse_historical_date(self, text):
        """Parse a single date for the Historical input. Reuses the
        same formats as the existing date search (today, yesterday,
        Nd, YYYY-MM-DD, M/D/YY, slash/dash interchangeable, 2/4-digit
        year). Range syntax (A..B) is rejected.

        Returns (date_obj, error_message). On success error is "".
        """
        s = (text or "").strip().lower()
        if not s:
            return None, "enter a date"
        if ".." in s:
            return None, "single date only (no range)"
        today = datetime.now().date()
        if s == "today":
            return today, ""
        if s == "yesterday":
            return today - timedelta(days=1), ""
        m = re.fullmatch(r"(\d+)\s*d", s)
        if m:
            n = int(m.group(1))
            if n < 0 or n > 365:
                return None, f"days out of range: {n}"
            return today - timedelta(days=n), ""
        d = self._try_iso(s)
        if d:
            return d, ""
        return None, "unrecognized date (try 2026-04-29, 4/29/26, today, 7d)"

    def _filter_wires_to_window(self, items, target_date,
                                  days_before=2, days_after=2):
        """Return a list of unified historical-result dicts for any
        rows in ``items`` (typically ``self.current_items``) whose
        date falls within ``[target_date - days_before, target_date + days_after]``.
        Items that already match a Polygon row will be deduped later by
        the title-match step, so this keeps everything in-window."""
        if not items or target_date is None:
            return []
        lo = target_date - timedelta(days=days_before)
        hi = target_date + timedelta(days=days_after)
        out = []
        for it in items:
            d_str = (it.get("date") or "").strip()
            if not d_str:
                continue
            try:
                d = datetime.strptime(d_str, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if not (lo <= d <= hi):
                continue
            t_str = (it.get("time") or "").strip()
            # Use the same "when" shape as Polygon/EDGAR rows so all
            # three sort by the same key. Use UTC suffix for parity —
            # we don't have real timezone info so this is a marker only.
            when = f"{d_str}T{(t_str or '00:00')}:00Z" if t_str else f"{d_str}"
            out.append({
                "source": "wires",
                "when": when,
                "type": it.get("source", "Wire") or "Wire",
                "title": it.get("headline", "") or "",
                "url": it.get("url", "") or "",
                "extra": {},
            })
        return out

    def run_historical_lookup(self):
        """User clicked the Historical Lookup button. Validates the
        date, takes over the wires Treeview, and kicks off Polygon +
        EDGAR queries on a daemon thread. Results render via after()
        on the main thread when both calls return."""
        if self._historical_busy:
            return
        sym = self.current_symbol
        if not sym or sym == "—":
            self._show_historical_error("no active symbol")
            return
        d, err = self._parse_historical_date(
            self.entry_historical_date.get(),
        )
        if err:
            self._show_historical_error(err)
            return
        self._show_historical_error("")
        self.historical_date = d
        self.historical_active = True
        self.historical_results = []
        self._historical_busy = True
        with self._gen_lock:
            self._historical_gen += 1
            gen = self._historical_gen
        # Clear the enriched-row promotion list — a new lookup starts
        # with no rows promoted; the enrichment pass will repopulate
        # it as data lands.
        self._historical_enriched_iids = []

        # Switch the Treeview into historical layout immediately and
        # show a "Loading…" placeholder row so the user sees the
        # takeover even before the network calls return.
        self._apply_historical_tree_columns()
        self.tree.delete(*self.tree.get_children())
        self.tree.insert(
            "", "end", iid="hist_loading",
            values=("", "", "...", f"Loading historical data for {sym} on {d.isoformat()}…"),
            tags=("hist_loading",),
        )
        self.btn_historical_lookup.config(state="disabled", text="…")

        # Populate the top earnings panel from the parquet for this date
        # window immediately (local + synchronous) — identical look to
        # the landing page. Blank if the parquet has no report in window.
        self.refresh_meta_label()

        api_key = _keyring_get_polygon()
        cik = self.current_cik
        cik_padded = str(int(cik)).zfill(10) if cik else ""
        forms = (getattr(self, "historical_forms", "") or
                  DEFAULT_HISTORICAL_FORMS)
        max_tickers = int(getattr(self, "historical_polygon_max_tickers", 5))
        ua = HEADERS.get("User-Agent", UA_LIST[0])
        target_iso = d.isoformat()

        # Wires/RSS pass — purely in-memory filter against the items
        # the scanner already loaded for the active symbol. Computed
        # synchronously on the main thread before the worker starts so
        # we don't race the worker against the wires Treeview takeover.
        wires_results = self._filter_wires_to_window(
            self.current_items, d,
        )

        def worker():
            try:
                poly_results, poly_err = HistoricalLookup.polygon_news(
                    sym, target_iso, api_key,
                    max_tickers=max_tickers,
                )
                edgar_results, edgar_err = HistoricalLookup.edgar_fulltext(
                    cik_padded, target_iso, forms, ua,
                )
            except Exception as exc:
                # An unexpected fetch error must NOT silently kill this
                # daemon thread and strand the feature permanently 'busy'
                # with a disabled button (the re-entrancy guard keys off
                # _historical_busy). Marshal a finish that resets state
                # and surfaces the failure as a notice row instead.
                _log.warning("historical lookup worker failed: %s",
                             type(exc).__name__)
                poly_results, poly_err = [], ""
                edgar_results, edgar_err = [], f"lookup failed ({type(exc).__name__})"
            # Tk isn't thread-safe; on app close this can race with
            # destroy() and raise "main thread is not in main loop".
            # Swallow that — it just means the user closed the window
            # while a lookup was in flight.
            try:
                self.after(
                    0, lambda: self._finish_historical_lookup(
                        gen, sym, target_iso,
                        poly_results, poly_err,
                        edgar_results, edgar_err,
                        wires_results,
                    ),
                )
            except (RuntimeError, tk.TclError):
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _finish_historical_lookup(self, gen, sym, target_iso,
                                   poly_results, poly_err,
                                   edgar_results, edgar_err,
                                   wires_results=None):
        # Stale callback: a newer lookup or an Exit/refresh raced in
        # while this fetch was in flight. Drop these results WITHOUT
        # touching _historical_busy / the button — doing so here would
        # re-enable the button while a newer lookup is still fetching,
        # allowing a third overlapping lookup. The legitimate owner (the
        # newer lookup's own completion, or exit_historical_mode) manages
        # the flag and button.
        if gen != self._historical_gen or not self.historical_active:
            return
        notes = []
        if poly_err == "no_key":
            notes.append("Polygon disabled — no API key in keyring")
        elif poly_err == "bad_key":
            notes.append("Polygon: API key rejected (401)")
        elif poly_err == "rate_limit":
            notes.append("Polygon: rate limited (5/min on free tier)")
        elif poly_err:
            notes.append(f"Polygon error: {poly_err}")
        if edgar_err == "no_cik":
            notes.append("EDGAR disabled — no CIK resolved for this ticker")
        elif edgar_err == "ua_block":
            notes.append("EDGAR rejected User-Agent (403)")
        elif edgar_err == "rate_limit":
            notes.append("EDGAR: rate limited")
        elif edgar_err:
            notes.append(f"EDGAR error: {edgar_err}")
        # Dedupe by exact title match — EDGAR full-text occasionally
        # returns the same filing 10+ times when index shards overlap,
        # and cross-source dupes (Polygon + wires + EDGAR all pointing
        # at the same press release) are noise. Walk order = win order;
        # wires walked first since they're already validated in-app,
        # then Polygon, then EDGAR. Empty titles bypass the filter.
        seen_titles = set()
        def _dedupe(rows):
            kept = []
            for r in rows:
                t = (r.get("title") or "").strip()
                if not t:
                    kept.append(r)
                    continue
                if t in seen_titles:
                    continue
                seen_titles.add(t)
                kept.append(r)
            return kept

        # Sort each source independently by ``when`` descending so the
        # most-recent items land at the top of their group. Then
        # concatenate: wires → Polygon → EDGAR. Wires sit at the top
        # since they're items the scanner already verified for this
        # ticker; Polygon is broader external coverage; EDGAR is
        # official filings.
        wires_results = wires_results or []
        wires_sorted = sorted(wires_results,
                               key=lambda r: r.get("when", ""),
                               reverse=True)
        poly_sorted = sorted(poly_results,
                              key=lambda r: r.get("when", ""),
                              reverse=True)
        edgar_sorted = sorted(edgar_results,
                               key=lambda r: r.get("when", ""),
                               reverse=True)
        results = _dedupe(wires_sorted) + _dedupe(poly_sorted) + _dedupe(edgar_sorted)
        self.historical_results = results
        self._render_historical_results(sym, target_iso, results, notes)
        self._historical_busy = False
        self.btn_historical_lookup.config(state="normal", text="Lookup")
        # Kick off the EDGAR enrichment pass on a daemon thread — now
        # 1-liner text summaries for 8-K-style filings ONLY (YoY/surprise
        # value extraction was removed; earnings live in the top panel,
        # sourced from the parquet). Async so the baseline render stays
        # fast; rows update in place as snippets come back.
        self._kickoff_edgar_enrichment(gen, sym, results)

    def _render_historical_results(self, sym, target_iso, results, notes):
        """Populate the (already historical-mode) Treeview with results
        + a leading banner row + any error/notice rows."""
        self.tree.delete(*self.tree.get_children())
        self.tree.insert(
            "", "end", iid="hist_banner",
            values=("", "", "▼",
                     f"HISTORICAL: {sym} @ {target_iso}  "
                     f"({len(results)} result{'s' if len(results) != 1 else ''})"),
            tags=("hist_banner",),
        )
        for n in notes:
            self.tree.insert(
                "", "end",
                values=("", "", "!", n),
                tags=("hist_note",),
            )
        if not results:
            self.tree.insert(
                "", "end",
                values=("", "", "—", "[No filings or news found in window]"),
                tags=("hist_note",),
            )
            return
        for i, item in enumerate(results):
            when = item.get("when", "") or ""
            # Strip the time-of-day for display; full UTC stays in the
            # underlying dict for sorting and selection lookup.
            short_when = when[:10] if when else ""
            src = item.get("source", "")
            src_label = {
                "edgar": "EDGAR",
                "polygon": "Polygon",
                "wires": "Wires",
            }.get(src, src.upper())
            type_str = item.get("type", "") or ""
            title = item.get("title", "") or ""
            extra = item.get("extra", {}) or {}
            sentiment = extra.get("sentiment")
            tag = "hist_default"
            if src == "polygon":
                if sentiment == "positive":
                    tag = "hist_pos"
                elif sentiment == "negative":
                    tag = "hist_neg"
                else:
                    tag = "hist_neu"
                if sentiment:
                    sym_marker = {"positive": "[+]",
                                   "neutral": "[~]",
                                   "negative": "[-]"}.get(sentiment, "")
                    if sym_marker:
                        title = f"{sym_marker} {title}"
            elif src == "edgar":
                # Default tag; the enrichment pass may upgrade this row
                # to hist_yoy_pos / hist_yoy_neg / hist_yoy_mixed once
                # YoY %s have been computed (10-K/10-Q only).
                tag = "hist_edgar"
            elif src == "wires":
                tag = "hist_wires"
            self.tree.insert(
                "", "end", iid=f"hist_{i}",
                values=(short_when, src_label, type_str, title),
                tags=(tag,),
            )

    def exit_historical_mode(self):
        """Tear down historical mode — restore wires Treeview layout
        and re-render live wires. Triggered by Exit button, by
        change_symbol, and by manual_refresh."""
        if not self.historical_active and self._wires_col_widths is None:
            # Already in wires mode and nothing to restore.
            return
        self.historical_active = False
        self.historical_date = None
        self.historical_results = []
        self._historical_busy = False
        with self._gen_lock:
            self._historical_gen += 1  # invalidate any in-flight callback
        self._historical_enriched_iids = []
        self._show_historical_error("")
        self._hide_tooltip()
        self._restore_wires_tree_columns()
        try:
            self.btn_historical_lookup.config(state="normal", text="Lookup")
        except tk.TclError:
            pass
        # Repaint the wires Treeview from current_items.
        try:
            self.refresh_ui()
        except Exception:
            pass
        # Restore the top earnings labels to the live (current-quarter)
        # view now that historical_active is False.
        try:
            self.refresh_meta_label()
        except Exception:
            pass

    def _apply_historical_tree_columns(self):
        """Reconfigure the Treeview to a 4-column historical layout.
        Saves the current wires column widths so we can restore them
        on exit."""
        # Snapshot wires widths exactly once per session-in-historical.
        if self._wires_col_widths is None:
            saved = {}
            for col in ("date", "age", "headline"):
                try:
                    saved[col] = int(self.tree.column(col, "width"))
                except tk.TclError:
                    pass
            self._wires_col_widths = saved
        # Tk's ttk.Treeview supports reconfiguring its columns tuple
        # at runtime; existing children get cleared since their values
        # tuple no longer matches the schema.
        self.tree.delete(*self.tree.get_children())
        self.tree.configure(columns=("when", "src", "type", "title"))
        self.tree.heading("when", text="Date")
        self.tree.heading("src", text="Source")
        self.tree.heading("type", text="Type")
        self.tree.heading("title", text="Title / Description")
        self.tree.column("when", width=90, anchor="center", stretch=False)
        self.tree.column("src", width=70, anchor="center", stretch=False)
        self.tree.column("type", width=120, anchor="w", stretch=False)
        self.tree.column("title", width=600, anchor="w", stretch=True)

    def _kickoff_edgar_enrichment(self, gen, sym, results):
        """Spawn a daemon thread that attaches a 1-liner text summary to
        each 8-K-style EDGAR row (8-K, 6-K, NT 10-*, and their /A
        amendments). Each row updates in place via
        ``self.after(0, _refresh_historical_row)`` as its snippet comes
        back. ``gen`` is the historical-lookup generation; if it changes
        mid-flight (user exits / refreshes / changes symbol), the worker
        bails on its next iteration.

        YoY/surprise value extraction (XBRL + parquet, per filing row)
        was removed — the earnings numbers now live in the top panel,
        sourced purely from the parquet for the looked-up date window."""
        # Quick filter: only EDGAR rows participate. If there are
        # none, skip the thread entirely.
        edgar_indices = [
            i for i, r in enumerate(results) if r.get("source") == "edgar"
        ]
        if not edgar_indices:
            return
        ua = HEADERS.get("User-Agent", UA_LIST[0])

        # Snapshot just what the worker needs so we don't reach back
        # into shared state from the daemon thread.
        snapshot = []
        for i in edgar_indices:
            r = results[i]
            extra = r.get("extra") or {}
            snapshot.append({
                "i": i,
                "form": (extra.get("form") or r.get("type") or "").upper(),
                "accession": extra.get("accession") or "",
                "url": r.get("url") or "",
            })

        ENRICHABLE_TEXT = {
            "8-K", "8-K/A", "6-K", "6-K/A",
            "NT 10-K", "NT 10-Q", "NT 10-K/A", "NT 10-Q/A",
        }

        def worker():
            for entry in snapshot:
                if gen != self._historical_gen:
                    return
                # Per-entry isolation + logging: one bad filing must not
                # silently abort enrichment of the remaining rows.
                try:
                    form = entry["form"]
                    idx = entry["i"]
                    if form in ENRICHABLE_TEXT:
                        accession = entry["accession"]
                        snippet, full = self._oneliner_get_or_fetch(
                            accession, entry["url"], ua,
                        )
                        if not snippet:
                            continue
                        self._post_enrichment(gen, idx, {
                            "oneliner": snippet,
                            "oneliner_full": full,
                        })
                except Exception as exc:
                    _log.debug("EDGAR enrichment row failed: %s",
                               type(exc).__name__)
                    continue

        threading.Thread(target=worker, daemon=True).start()

    def _post_enrichment(self, gen, idx, enrichment):
        """Daemon-thread bridge: post a single row enrichment back to
        the main thread. ``enrichment`` is a dict with keys 'yoy' or
        'oneliner'. Tk's after() must be called from the main thread
        in principle but the existing codebase already does this
        cross-thread pattern in bg_fetch — wrap defensively to
        swallow the destroy-race."""
        try:
            self.after(
                0,
                lambda: self._refresh_historical_row(gen, idx, enrichment),
            )
        except (RuntimeError, tk.TclError):
            pass

    def _refresh_historical_row(self, gen, idx, enrichment):
        """Apply a 1-liner enrichment to a single historical row and
        rewrite its Treeview title in place. Bails if the lookup
        generation has rolled forward (user exited / re-ran).

        (Earnings YoY/surprise enrichment was removed; those values now
        live in the top earnings panel. This only appends the 8-K-style
        text snippet, so no row promotion happens.)"""
        if gen != self._historical_gen or not self.historical_active:
            return
        if not (0 <= idx < len(self.historical_results)):
            return
        row = self.historical_results[idx]
        # Persist enrichment into the underlying dict so dedup /
        # double-click / re-render still see it.
        row.setdefault("enrichment", {}).update(enrichment)
        # Recompute display title from base + enrichments.
        base_title = row.get("title") or ""
        enr = row["enrichment"]
        suffix_parts = []
        tag = "hist_edgar"
        has_data_enrichment = False
        if "oneliner" in enr and enr["oneliner"]:
            suffix_parts.append(f"— {enr['oneliner']}")
        if suffix_parts:
            new_title = f"{base_title}  " + "  ".join(suffix_parts)
        else:
            new_title = base_title
        # Existing values tuple is (when, src, type, title) — preserve
        # the first three, swap title.
        iid = f"hist_{idx}"
        try:
            cur_vals = self.tree.item(iid, "values")
        except tk.TclError:
            return
        if not cur_vals or len(cur_vals) < 4:
            return
        new_vals = (cur_vals[0], cur_vals[1], cur_vals[2], new_title)
        try:
            self.tree.item(iid, values=new_vals, tags=(tag,))
        except tk.TclError:
            pass
        # Promote rows that picked up actual data above the rest of
        # the results. Banner + notes stay anchored at the top; the
        # promoted block sits immediately after them, sorted within
        # itself by ``when`` desc. Rows without data enrichment keep
        # their original position.
        if has_data_enrichment:
            self._promote_enriched_row(iid)

    def _promote_enriched_row(self, iid):
        """Move ``iid`` into the enriched-rows block at the top of the
        results section (after the banner + any notes), keeping that
        block sorted by ``when`` desc.

        Tracked via ``self._historical_enriched_iids`` so we can
        rebuild the block ordering each time a new row comes in.
        Uses ``tree.move`` so scroll position / selection are
        preserved (re-inserting would jump the user)."""
        try:
            children = list(self.tree.get_children(""))
        except tk.TclError:
            return
        if iid not in children:
            return
        # Initialize / refresh the tracked set. Walk current children
        # to find the boundary between notes and results so we know
        # where to insert. Notes are tagged hist_note / hist_banner /
        # hist_loading; result rows have hist_* tags pointing to the
        # actual data (hist_default / hist_edgar / hist_wires / hist_pos
        # / hist_neg / hist_neu / hist_yoy_* etc.).
        non_data_tags = {"hist_banner", "hist_note", "hist_loading"}
        insert_at = 0
        for i, child in enumerate(children):
            try:
                tags = self.tree.item(child, "tags") or ()
            except tk.TclError:
                continue
            if tags and tags[0] in non_data_tags:
                insert_at = i + 1
            else:
                break
        if not hasattr(self, "_historical_enriched_iids"):
            self._historical_enriched_iids = []
        # Drop stale tracking entries that no longer exist in the tree
        # (defensive against historical_active flips).
        self._historical_enriched_iids = [
            x for x in self._historical_enriched_iids if x in children
        ]
        if iid in self._historical_enriched_iids:
            self._historical_enriched_iids.remove(iid)
        self._historical_enriched_iids.append(iid)
        # Sort the enriched block by ``when`` desc; un-promoted rows
        # keep their existing relative order below the block.
        def _when_of(x):
            try:
                i = int(x.split("_", 1)[1])
            except (ValueError, IndexError):
                return ""
            if 0 <= i < len(self.historical_results):
                return self.historical_results[i].get("when", "") or ""
            return ""
        self._historical_enriched_iids.sort(key=_when_of, reverse=True)
        # Apply moves: walk the sorted list and `tree.move` each to its
        # target slot. Idempotent — a row that's already at its target
        # index is a no-op.
        for offset, target_iid in enumerate(self._historical_enriched_iids):
            try:
                self.tree.move(target_iid, "", insert_at + offset)
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    # Hover tooltip — shows enriched detail for any historical-mode row
    # that has either an auto-generated 1-liner OR a YoY/Surprise data
    # block. The 1-liner gets a wider blurb beyond the truncated row
    # title; YoY/Surprise data gets a per-value colored breakdown so
    # users can see the green/red surprise palette that the inline row
    # color (limited to whole-row tinting) can't represent.
    # ------------------------------------------------------------------
    _TOOLTIP_DELAY_MS = 350     # hover dwell before showing
    _TOOLTIP_WRAP_PX = 520
    _YOY_POS_COLOR = "#00BFFF"  # deep sky blue (matches earnings chart)
    _YOY_NEG_COLOR = "#FF1493"  # deep pink   (matches earnings chart)
    _YOY_WEAK_COLOR = "#888888" # muted grey — YoY built on a near-zero
                                # prior-year base; the % is unreliable so
                                # it renders neutral instead of blue/pink.
    _SURP_POS_COLOR = "#00CC00" # bright green
    _SURP_NEG_COLOR = "#FF4040" # bright red

    # Earnings-chart-popup color overrides (user-editable via the chart's
    # "Colors…" settings button, persisted in scanner_settings.json,
    # applied to ALL popouts). These are SEPARATE from the class
    # constants above (which still drive the main scanner row) so editing
    # popout colors never disturbs the landing page. Defaults mirror the
    # class constants; the value-label default is "" = follow the date
    # tick / foreground color. ``_chart_color_defaults`` is the single
    # source of truth for both the load-time defaults and the dialog's
    # per-color "reset to default".
    _CHART_SEL_DEFAULT = "#FFE600"   # click-highlight yellow
    _CHART_HIST_DEFAULT = "#9C27B0"  # historical-match purple

    def _chart_color_defaults(self):
        return {
            "_chart_yoy_pos": self._YOY_POS_COLOR,
            "_chart_yoy_neg": self._YOY_NEG_COLOR,
            "_chart_surp_pos": self._SURP_POS_COLOR,
            "_chart_surp_neg": self._SURP_NEG_COLOR,
            "_chart_label_color": "",   # "" => follow the date/fg color
            "_chart_sel_color": self._CHART_SEL_DEFAULT,
            "_chart_hist_color": self._CHART_HIST_DEFAULT,
        }

    # Maps the persisted JSON key <-> the instance attr for chart colors.
    _CHART_COLOR_KEYS = (
        ("chart_yoy_pos_color",  "_chart_yoy_pos"),
        ("chart_yoy_neg_color",  "_chart_yoy_neg"),
        ("chart_surp_pos_color", "_chart_surp_pos"),
        ("chart_surp_neg_color", "_chart_surp_neg"),
        ("chart_label_color",    "_chart_label_color"),
        ("chart_sel_color",      "_chart_sel_color"),
        ("chart_hist_color",     "_chart_hist_color"),
    )

    # A YoY % is flagged "weak" (rendered grey) when the prior-year
    # same-quarter reported value it was divided by is below these
    # magnitudes — that tiny denominator is what produces the absurd
    # ±hundreds-of-% blowups for near-breakeven names (e.g. AAOI).
    _YOY_SMALL_BASE_EPS = 0.05   # |prior-year reported EPS|  < $0.05
    _YOY_SMALL_BASE_REV = 1.0    # |prior-year reported Rev|  < $1.0M

    # A SURPRISE % is flagged "weak" when the analyst ESTIMATE it was
    # divided by is near zero (same tiny-denominator blowup, different
    # base). Weak surprises keep their green/red color but get an "(s)"
    # marker in labels/table and a grey "s" inside their graph bar.
    _SURP_SMALL_BASE_EPS = 0.05  # |estimated EPS|  < $0.05
    _SURP_SMALL_BASE_REV = 1.0   # |estimated Rev|  < $1.0M

    def _on_tree_hover(self, event):
        if not self.historical_active:
            self._hide_tooltip()
            return
        try:
            iid = self.tree.identify_row(event.y)
        except tk.TclError:
            iid = ""
        if not iid or not iid.startswith("hist_") or iid in (
            "hist_banner", "hist_loading",
        ):
            self._hide_tooltip()
            return
        if iid == self._tooltip_iid:
            return  # already showing for this row
        # Different row (or no tooltip currently shown) — schedule a
        # re-display after a brief dwell so quick mouse passes don't
        # spam tooltip windows.
        self._hide_tooltip()
        self._tooltip_iid = iid
        try:
            x_root, y_root = event.x_root, event.y_root
        except AttributeError:
            return
        self._tooltip_after = self.after(
            self._TOOLTIP_DELAY_MS,
            lambda: self._show_tooltip(iid, x_root, y_root),
        )

    def _show_tooltip(self, iid, x_root, y_root):
        self._tooltip_after = None
        if not self.historical_active or self._tooltip_iid != iid:
            return
        try:
            idx = int(iid.split("_", 1)[1])
        except (ValueError, IndexError):
            return
        if not (0 <= idx < len(self.historical_results)):
            return
        row = self.historical_results[idx]
        enr = row.get("enrichment") or {}
        full = (enr.get("oneliner_full") or "").strip()
        yoy = enr.get("yoy") or {}
        has_yoy_data = any(yoy.get(k) is not None for k in
                            ("rev_yoy", "eps_yoy", "rev_surp", "eps_surp"))
        # Show the tooltip if EITHER a 1-liner OR YoY/surprise data
        # exists. (No tooltip for plain Polygon / wires / Form 4 rows
        # — there's nothing extra to surface.)
        if not full and not has_yoy_data:
            return
        # Borderless, always-on-top tooltip Toplevel positioned just
        # below+right of the cursor.
        c = self.colors
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        try:
            tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        tip.geometry(f"+{x_root + 14}+{y_root + 18}")
        # 1-px outline frame so the tip reads as a discrete object on
        # both light and dark themes.
        outline = tk.Frame(tip, bg=c["FG"])
        outline.pack()
        body = tk.Frame(outline, bg=c["BG"], padx=8, pady=6)
        body.pack(padx=1, pady=1)
        # Header line with form + filed-date for context.
        extra = row.get("extra") or {}
        header_bits = []
        form = extra.get("form") or row.get("type") or ""
        if form:
            header_bits.append(form)
        fd = extra.get("file_date") or ""
        if fd:
            header_bits.append(f"filed {fd}")
        if header_bits:
            tk.Label(
                body, text="  •  ".join(header_bits),
                bg=c["BG"], fg=c["CREDIT"],
                font=("Segoe UI", max(7, self.base_font_size - 1)),
            ).pack(anchor="w")
        # Per-value colored YoY + Surprise breakdown (when available).
        # This is the "real" colored layout the inline Treeview row
        # can't render. Each metric gets its own Label so we can hit
        # blue/pink for YoY and green/red for surprise.
        if has_yoy_data:
            self._render_tooltip_yoy(body, yoy)
        # The 1-liner blurb (when available).
        if full:
            tk.Label(
                body, text=full, bg=c["BG"], fg=c["FG"],
                font=("Segoe UI", self.base_font_size),
                wraplength=self._TOOLTIP_WRAP_PX, justify="left", anchor="w",
            ).pack(anchor="w", pady=(4, 0))
        self._tooltip_win = tip

    def _render_tooltip_yoy(self, body, yoy):
        """Render a 2-row YoY+Surprise grid into the tooltip body
        with per-value colors: blue/pink for YoY, green/red for
        Surprise. Missing values render as muted '—'."""
        c = self.colors
        std = ("Segoe UI", self.base_font_size)
        bold = ("Segoe UI", self.base_font_size, "bold")

        def _color_for(v, pos_color, neg_color):
            if v is None:
                return c["CREDIT"]
            if v > 0:
                return pos_color
            if v < 0:
                return neg_color
            return c["FG"]

        def _fmt(v):
            return f"{v:+.1f}%" if v is not None else "—"

        grid = tk.Frame(body, bg=c["BG"])
        grid.pack(anchor="w", pady=(6, 0))
        # Two rows: YoY %s and Surprise %s. Each row has a label
        # column + EPS column + Rev column.
        rows = [
            ("YoY",      yoy.get("eps_yoy"),  yoy.get("rev_yoy"),
             self._YOY_POS_COLOR, self._YOY_NEG_COLOR),
            ("Surprise", yoy.get("eps_surp"), yoy.get("rev_surp"),
             self._SURP_POS_COLOR, self._SURP_NEG_COLOR),
        ]
        for r, (name, eps_v, rev_v, pos_c, neg_c) in enumerate(rows):
            tk.Label(grid, text=f"{name}:", bg=c["BG"], fg=c["FG"],
                     font=std, anchor="e").grid(
                row=r, column=0, padx=(0, 8), sticky="e",
            )
            tk.Label(grid, text=f"EPS {_fmt(eps_v)}",
                     bg=c["BG"], fg=_color_for(eps_v, pos_c, neg_c),
                     font=bold, anchor="w").grid(
                row=r, column=1, padx=(0, 12), sticky="w",
            )
            tk.Label(grid, text=f"Rev {_fmt(rev_v)}",
                     bg=c["BG"], fg=_color_for(rev_v, pos_c, neg_c),
                     font=bold, anchor="w").grid(
                row=r, column=2, sticky="w",
            )

    def _hide_tooltip(self):
        if self._tooltip_after is not None:
            try:
                self.after_cancel(self._tooltip_after)
            except tk.TclError:
                pass
            self._tooltip_after = None
        if self._tooltip_win is not None:
            try:
                self._tooltip_win.destroy()
            except tk.TclError:
                pass
            self._tooltip_win = None
        self._tooltip_iid = None

    def _restore_wires_tree_columns(self):
        """Re-apply the original 3-column wires layout."""
        self.tree.delete(*self.tree.get_children())
        self.tree.configure(columns=("date", "age", "headline"))
        self.tree.heading("date", text="Date")
        self.tree.heading("age", text="Age")
        self.tree.heading("headline", text="Headline")
        self.tree.column("date", width=80, anchor="center", stretch=False)
        self.tree.column("age", width=70, anchor="center", stretch=False)
        self.tree.column("headline", width=500, anchor="w", stretch=True)
        for col, w in (self._wires_col_widths or {}).items():
            try:
                self.tree.column(col, width=int(w))
            except tk.TclError:
                pass
        self._wires_col_widths = None

    # Accepted date formats. Slash and dash separators are
    # interchangeable (we normalize to '-' before parsing). For
    # 2-digit years, Python's %y maps 00-68 -> 2000-2068 and
    # 69-99 -> 1969-1999, which is fine for typical use.
    _DATE_FORMATS = (
        "%Y-%m-%d",   # 2026-04-29
        "%y-%m-%d",   # 26-04-29
        "%m-%d-%Y",   # 04-29-2026
        "%m-%d-%y",   # 04-29-26
        "%Y-%m",      # 2026-04 (treated as the 1st of that month)
    )

    @classmethod
    def _try_iso(cls, text):
        s = text.strip().replace("/", "-")
        if not s:
            return None
        for fmt in cls._DATE_FORMATS:
            try:
                return datetime.strptime(s, fmt).date()
            except (ValueError, TypeError):
                continue
        return None

    def _parse_date_filter(self, text):
        """Parse the Date: input. Returns (predicate, error_message).
        Supported forms (slash or dash interchangeable; 4- or 2-digit year):
            2026-04-29  /  2026/04/29  /  04/29/2026  /  04/29/26  — exact date
            today                                                  — today
            yesterday                                              — yesterday
            7d                                                     — within last N days (inclusive of today)
            2026-04-01..2026-04-29  /  04/01/26..04/29/26          — inclusive range
        """
        s = text.strip().lower()
        today = datetime.now().date()
        if s == "today":
            return (lambda d: d == today.isoformat()), None
        if s == "yesterday":
            yest = (today - timedelta(days=1)).isoformat()
            return (lambda d: d == yest), None
        m = re.fullmatch(r"(\d+)\s*d", s)
        if m:
            n = int(m.group(1))
            if n < 0 or n > 365:
                return None, f"days out of range: {n}"
            cutoff = (today - timedelta(days=n)).isoformat()
            today_iso = today.isoformat()
            return (lambda d: cutoff <= d <= today_iso), None
        if ".." in s:
            parts = s.split("..", 1)
            lo = self._try_iso(parts[0])
            hi = self._try_iso(parts[1])
            if not lo or not hi:
                return None, "bad range (use YYYY-MM-DD..YYYY-MM-DD or M/D/YY..M/D/YY)"
            if lo > hi:
                lo, hi = hi, lo
            lo_iso, hi_iso = lo.isoformat(), hi.isoformat()
            return (lambda d: lo_iso <= d <= hi_iso), None
        exact = self._try_iso(s)
        if exact:
            iso = exact.isoformat()
            return (lambda d: d == iso), None
        return None, "unrecognized date (try 2026-04-29, 4/29/26, today, 7d, A..B)"

    def _retag_visible_rows(self):
        """Recompute hot/old/today/old tags for the rows already in the
        Treeview without re-inserting anything. Cheap and avoids the
        scroll-jump that ``tree.delete + tree.insert`` produces."""
        for idx in self._displayed_indices:
            try:
                item = self.current_items[idx]
            except (IndexError, TypeError):
                continue
            tag = "today" if item.get("is_today") else "old"
            headline = item.get("headline", "")
            headline_lower = headline.lower()
            if self._match_hot_words(headline, headline_lower, self.hot_words_new):
                tag = "hot_new"
            elif self._match_hot_words(headline, headline_lower, self.hot_words_old):
                tag = "hot_old"
            try:
                self.tree.item(str(idx), tags=(tag,))
            except tk.TclError:
                pass

    def _match_hot_words(self, headline, headline_lower, terms):
        for kind, pattern in terms:
            if kind == "exact":
                if pattern.search(headline):
                    return True
            else:
                if pattern in headline_lower:
                    return True
        return False

    def refresh_ui(self):
        # Historical mode owns the Treeview — skip the wires rebuild
        # so the lookup results stay on screen until the user exits,
        # refreshes, or switches symbols.
        if self.historical_active:
            return
        self.tree.delete(*self.tree.get_children())
        self._displayed_indices = []
        today_date = datetime.now().date()
        yesterday_str = (today_date - timedelta(days=1)).isoformat()
        # Search filters operate on the *base* visible set chosen by
        # the 48h/All/today radios, then narrow further by keyword
        # and/or date if the user has any active. Empty search → all
        # items in the base set are shown (current behavior).
        kw_terms = self.search_keywords
        date_pred = self.search_date_pred
        for idx, item in enumerate(self.current_items):
            show = False
            if self.var_all.get(): show = True
            elif self.var_48.get():
                if item['date'] >= yesterday_str: show = True
            else:
                if item['is_today']: show = True
            if not show: continue
            if kw_terms:
                head = item.get('headline', '')
                if not self._match_hot_words(head, head.lower(), kw_terms):
                    continue
            if date_pred is not None:
                d = item.get('date', '')
                if not d or not date_pred(d):
                    continue
            tag = "today" if item['is_today'] else "old"
            headline_lower = item['headline'].lower()
            # New wins over Old when both match (leftmost-input priority).
            if self._match_hot_words(item['headline'], headline_lower, self.hot_words_new):
                tag = "hot_new"
            elif self._match_hot_words(item['headline'], headline_lower, self.hot_words_old):
                tag = "hot_old"
            self._displayed_indices.append(idx)
            self.tree.insert("", "end", values=(item['date'], item['age'] or item['time'], item['headline']), tags=(tag,), iid=str(idx))

    # Whitelist of URL schemes we trust to hand to the OS browser.
    # Defense against `javascript:`, `file:`, `vbscript:`, or custom
    # protocol-handler URLs that could slip in from a hostile RSS
    # feed, Polygon article, or scraped Finviz/EDGAR row.
    _ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

    def _safe_open_url(self, url):
        if not url or not isinstance(url, str):
            return
        try:
            scheme = urlparse(url).scheme.lower()
        except (ValueError, TypeError):
            return
        if scheme not in self._ALLOWED_URL_SCHEMES:
            return
        try:
            webbrowser.open(url)
        except (OSError, webbrowser.Error):
            pass

    def open_sec_shelf_link(self, event=None):
        if not self.current_symbol or self.current_symbol == "—": return
        query = self.current_cik if self.current_cik else self.current_symbol
        safe_query = url_quote(str(query), safe="")
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={safe_query}&type=S-3&dateb=&owner=exclude&count=10"
        self._safe_open_url(url)

    def open_sec_recent(self, event=None):
        if not self.current_symbol or self.current_symbol == "—": return
        query = self.current_cik if self.current_cik else self.current_symbol
        safe_query = url_quote(str(query), safe="")
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={safe_query}&count=10"
        self._safe_open_url(url)

    def open_finviz_link(self, event=None):
        if not self.current_symbol or self.current_symbol == "—": return
        safe_sym = url_quote(str(self.current_symbol), safe="")
        url = f"https://finviz.com/quote.ashx?t={safe_sym}"
        self._safe_open_url(url)

    # ---- ETF indicator -----------------------------------------------------

    def _update_etf_label(self, symbol):
        """Repaint the ETF indicator for the given symbol.

        Three states:
          - symbol is a known underlying with tracking ETFs → "ETF: YES" white
          - symbol IS one of the leveraged ETFs → "ETF: {UND} {mult}x" blue
          - otherwise → "ETF: NO" gray
        """
        # The label is constructed late in __init__ relative to the
        # first settings load; guard so an early reload can't crash.
        lbl = getattr(self, "lbl_etf", None)
        if lbl is None:
            return
        c = self.colors
        # Always recompute the second ("Held") indicator alongside the
        # primary one — they describe the same symbol from two angles.
        self._update_etf_hold_label(symbol)
        # A symbol change invalidates any open ETF-self tooltip.
        self._hide_etf_holdings_tip()
        if not symbol or symbol == "—" or self.etf_map is None:
            lbl.config(text="ETF: —", fg=c["CREDIT"], cursor="")
            return
        # 1) Single-stock leveraged ETF (etf_map reverse) — highest priority.
        rev = self.etf_map.get_underlying_for(symbol)
        if rev:
            text = f"ETF: {rev['underlying']} {format_mult(rev['mult'])}"
            lbl.config(text=text, fg=c["ETF_BLUE"], cursor="hand2")
            return
        # 2) Multi-holding ETF (sector/index/thematic/leveraged-index). Blue,
        #    with a high-confidence sector/strategy prefix + leverage when
        #    available (e.g. "ETF: Tech", "ETF: 3X", "ETF: Industrials 3X").
        prof = self.etf_holdings.get_profile(symbol) if self.etf_holdings else None
        if prof:
            lbl.config(text=self._etf_self_text(prof), fg=c["ETF_BLUE"],
                       cursor="hand2")
            return
        # 3) Otherwise it's a stock: does the single-stock map track it?
        etfs = self.etf_map.get_etfs_for(symbol)
        if etfs:
            lbl.config(text="ETF: YES", fg=c["FG"], cursor="hand2")
            return
        lbl.config(text="ETF: NO", fg=c["CREDIT"], cursor="")

    @staticmethod
    def _etf_self_text(prof) -> str:
        """Indicator text for a symbol that IS a multi-holding ETF:
        ``ETF: [<sector>] [<mult>X]`` — omitting whichever part is absent."""
        parts = []
        label = (prof.get("sector_label") or "").strip()
        if label:
            parts.append(label)
        mult = prof.get("mult")
        if mult:
            parts.append(format_mult(mult).upper())  # "3x" -> "3X"
        return "ETF: " + " ".join(parts) if parts else "ETF:"

    def _update_etf_hold_label(self, symbol):
        """Repaint the second 'Held' indicator: count of multi-holding ETFs
        that hold ``symbol``. Blank when none, the symbol is itself an ETF,
        or holdings data isn't available."""
        lbl = getattr(self, "lbl_etf_hold", None)
        if lbl is None:
            return
        c = self.colors
        if (not symbol or symbol == "—" or self.etf_holdings is None
                or self.etf_holdings.is_etf(symbol)
                or (self.etf_map is not None
                    and self.etf_map.get_underlying_for(symbol))):
            lbl.config(text="", cursor="")
            return
        holders = self.etf_holdings.get_holders_for(symbol)
        if holders:
            lbl.config(text=f"Held: {len(holders)}", fg=c["ETF_BLUE"],
                       cursor="hand2")
        else:
            lbl.config(text="", cursor="")

    def _on_etf_label_click(self, event=None):
        """Dispatch the click based on the current indicator state.

        - Single-stock ETF → open Finviz for the underlying it tracks
        - Multi-holding ETF → popup listing its top-N constituents
        - Underlying-with-ETFs → dropdown popup listing tracking ETFs
        - No data → no action
        """
        sym = self.current_symbol
        if not sym or sym == "—" or self.etf_map is None:
            return
        rev = self.etf_map.get_underlying_for(sym)
        if rev:
            safe = url_quote(str(rev["underlying"]), safe="")
            self._safe_open_url(f"https://finviz.com/quote.ashx?t={safe}")
            return
        prof = self.etf_holdings.get_profile(sym) if self.etf_holdings else None
        if prof:
            self._show_etf_constituents_popup(sym, prof, event)
            return
        etfs = self.etf_map.get_etfs_for(sym)
        if not etfs:
            return
        self._show_etf_popup(sym, etfs, event)

    def _on_etf_hold_label_click(self, event=None):
        """Click on the 'Held: N' indicator → popup of the multi-holding
        ETFs that hold the active stock."""
        sym = self.current_symbol
        if not sym or sym == "—" or self.etf_holdings is None:
            return
        holders = self.etf_holdings.get_holders_for(sym)
        if not holders:
            return
        self._show_etf_holders_popup(sym, holders, event)

    # ---- ETF-self constituent tooltip ------------------------------------

    def _on_etf_label_enter(self, event=None):
        sym = self.current_symbol
        if not sym or sym == "—" or self.etf_holdings is None:
            return
        prof = self.etf_holdings.get_profile(sym)
        if not prof:
            return
        self._show_etf_holdings_tip(sym, prof)

    def _on_etf_label_leave(self, event=None):
        self._hide_etf_holdings_tip()

    def _hide_etf_holdings_tip(self):
        win = getattr(self, "_etf_tip_win", None)
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass
            self._etf_tip_win = None

    def _show_etf_holdings_tip(self, sym, prof):
        """Borderless tooltip listing the ETF's constituents alphabetically
        (top-N from the source). Anchored just below the ETF indicator."""
        self._hide_etf_holdings_tip()
        holdings = prof.get("holdings") or []
        if not holdings:
            return
        c = self.colors
        names = sorted({h["ticker"] for h in holdings})
        count = prof.get("count") or len(names)
        shown = len(names)
        header = sym
        mult = prof.get("mult")
        if prof.get("sector_label"):
            header += f" · {prof['sector_label']}"
        if mult:
            header += f" · {format_mult(mult).upper()}"
        cap = f"Top {shown} of {count} holdings" if count > shown else \
              f"{shown} holding(s)"
        if prof.get("date"):
            cap += f"  ({prof['date']})"
        # Wrap the ticker list into rows of ~8 for a compact tooltip.
        rows = [", ".join(names[i:i + 8]) for i in range(0, len(names), 8)]
        body = "\n".join(rows)
        try:
            tip = tk.Toplevel(self)
            tip.wm_overrideredirect(True)
            try:
                tip.attributes("-topmost", True)
            except tk.TclError:
                pass
            frame = tk.Frame(tip, bg=c["ACCENT"], bd=1, relief="solid")
            frame.pack()
            tk.Label(frame, text=header, bg=c["ACCENT"], fg=c["ETF_BLUE"],
                     font=("Segoe UI", max(8, self.base_font_size - 1), "bold"),
                     anchor="w", justify="left").pack(fill="x", padx=6, pady=(4, 0))
            tk.Label(frame, text=cap, bg=c["ACCENT"], fg=c["CREDIT"],
                     font=("Segoe UI", max(7, self.base_font_size - 2)),
                     anchor="w", justify="left").pack(fill="x", padx=6)
            tk.Label(frame, text=body, bg=c["ACCENT"], fg=c["FG"],
                     font=("Consolas", max(7, self.base_font_size - 2)),
                     anchor="w", justify="left").pack(fill="x", padx=6, pady=(2, 4))
            # Position under the indicator.
            x = self.lbl_etf.winfo_rootx()
            y = self.lbl_etf.winfo_rooty() + self.lbl_etf.winfo_height() + 2
            tip.wm_geometry(f"+{x}+{y}")
            self._etf_tip_win = tip
        except tk.TclError:
            self._etf_tip_win = None

    def _open_etf_refresh_dialog(self, pending_path_str: str):
        """Modal terminal-style window that streams scrape progress live.

        ``pending_path_str`` is whatever's currently in the path entry
        of the Settings dialog — used so a Refresh triggered before the
        user clicks Save still writes to the path they typed. Empty
        falls back to the default.
        """
        # Local import keeps tk startup fast and the dependency on the
        # scraper module limited to refresh time only.
        from tkinter import scrolledtext
        import threading
        c = self.colors
        win = tk.Toplevel(self)
        win.title("Refresh ETF map")
        win.configure(bg=c["BG"])
        win.transient(self)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        win.geometry("780x420")
        std = ("Segoe UI", self.base_font_size)
        mono = ("Consolas", max(8, self.base_font_size - 1))
        btn_conf = {"bg": c["BTN_BG"], "fg": c["BTN_FG"]}

        wrap = tk.Frame(win, bg=c["BG"])
        wrap.pack(fill="both", expand=True, padx=10, pady=8)

        target_path = Path(pending_path_str) if pending_path_str else ETF_MAP_DEFAULT_PATH
        tk.Label(
            wrap,
            text=f"Writing to: {target_path}",
            font=std, bg=c["BG"], fg=c["CREDIT"], anchor="w", justify="left",
        ).pack(fill="x", anchor="w", pady=(0, 4))

        log_widget = scrolledtext.ScrolledText(
            wrap, font=mono, bg="#0E0E0E", fg="#E0E0E0",
            insertbackground="#E0E0E0", height=18, wrap="word",
        )
        log_widget.pack(fill="both", expand=True)
        log_widget.configure(state="disabled")

        btn_close = tk.Button(
            wrap, text="Close", command=win.destroy, font=std, **btn_conf,
            state="disabled",
        )
        btn_close.pack(anchor="e", pady=(6, 0))

        # Thread-safe append: scraper thread calls _append(line); we
        # marshal back onto the Tk main loop with after(0, ...).
        def _append(line: str) -> None:
            try:
                log_widget.configure(state="normal")
                log_widget.insert("end", line + "\n")
                log_widget.see("end")
                log_widget.configure(state="disabled")
            except tk.TclError:
                pass  # window destroyed mid-run

        def progress_cb(line: str) -> None:
            # Called from the scraper thread. Marshal onto Tk thread.
            try:
                win.after(0, _append, line)
            except tk.TclError:
                pass

        def worker():
            try:
                from etf_scraper import scrape_all
                # Pass the existing forward map so an issuer whose
                # scrape fails (404, layout change, etc.) preserves
                # its prior entries instead of being silently dropped.
                existing = {}
                try:
                    if self.etf_map is not None:
                        # Public, lock-guarded snapshot instead of reaching
                        # into the private _forward.
                        existing = self.etf_map.snapshot_forward()
                except Exception:  # noqa: BLE001
                    existing = {}
                forward, scraped, errors = scrape_all(
                    progress_cb, existing_forward=existing,
                )
            except Exception as exc:  # noqa: BLE001
                progress_cb(f"FATAL: {exc!r}")
                win.after(0, lambda: btn_close.config(state="normal"))
                return
            # Apply: write JSON via EtfMap.replace, swap the path first
            # if it changed, then refresh the indicator.
            try:
                if self.etf_map is None:
                    self.etf_map = EtfMap(path=target_path)
                else:
                    self.etf_map.set_path(target_path)
                self.etf_map.replace(
                    forward,
                    issuers_scraped=scraped,
                    errors=errors,
                )
                self.etf_map_custom_path = (
                    str(target_path) if target_path != ETF_MAP_DEFAULT_PATH else ""
                )
                progress_cb(f"Saved. Indicator updated.")
            except Exception as exc:  # noqa: BLE001
                progress_cb(f"SAVE ERROR: {exc!r}")
            # --- Multi-holding ETF holdings (stockanalysis.com) ---
            # Runs after the single-stock map on the SAME worker thread so
            # one Refresh updates both. Always written to the holdings map's
            # own default path (independent of the single-stock custom path).
            try:
                from etf_scraper import scrape_etf_holdings
                progress_cb("")
                progress_cb("=== Multi-holding ETF holdings ===")
                existing_profiles = {}
                try:
                    if self.etf_holdings is not None:
                        existing_profiles = self.etf_holdings.snapshot_profiles()
                except Exception:  # noqa: BLE001
                    existing_profiles = {}
                # Pass the SEC name->ticker resolver so swap-based funds
                # (SPCL and the leveraged/inverse families) recover real
                # constituents from their swap descriptions.
                _resolver = None
                try:
                    _resolver = self.fetcher.cik_resolver.resolve_name_to_ticker
                except AttributeError:
                    _resolver = None
                profiles, h_errors = scrape_etf_holdings(
                    progress_cb, existing_profiles=existing_profiles,
                    name_resolver=_resolver,
                )
                if self.etf_holdings is None:
                    self.etf_holdings = EtfHoldings()
                self.etf_holdings.replace(
                    profiles, source="stockanalysis.com", errors=h_errors,
                )
                progress_cb(f"Holdings saved ({len(profiles)} ETFs, "
                            f"{len(h_errors)} error(s)).")
            except Exception as exc:  # noqa: BLE001
                progress_cb(f"HOLDINGS ERROR: {exc!r}")
            # Repaint the indicator under the new map.
            def _finalize():
                self._update_etf_label(self.current_symbol)
                btn_close.config(state="normal")
            win.after(0, _finalize)

        threading.Thread(target=worker, daemon=True).start()
        win.bind("<Escape>", lambda _e: win.destroy() if str(btn_close["state"]) == "normal" else None)

    def _open_etf_health_dialog(self, pending_path_str: str):
        """Read-only popup with map stats + per-issuer counts."""
        c = self.colors
        win = tk.Toplevel(self)
        win.title("ETF map — health")
        win.configure(bg=c["BG"])
        win.transient(self)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        std = ("Segoe UI", self.base_font_size)
        std_bold = ("Segoe UI", self.base_font_size, "bold")
        small = ("Segoe UI", max(7, self.base_font_size - 1))
        btn_conf = {"bg": c["BTN_BG"], "fg": c["BTN_FG"]}

        wrap = tk.Frame(win, bg=c["BG"])
        wrap.pack(fill="both", expand=True, padx=12, pady=10)

        # Build a fresh EtfMap pointing at the pending path so the
        # health view reflects what the user has TYPED but not yet
        # saved — same UX as the rest of the Settings dialog where
        # previews update live.
        if pending_path_str:
            try:
                em = EtfMap(path=Path(pending_path_str))
            except Exception as exc:  # noqa: BLE001
                em = None
                tk.Label(
                    wrap, text=f"Failed to load: {exc}",
                    font=std, bg=c["BG"], fg=c["TXT_BAD"],
                ).pack(anchor="w")
        else:
            em = self.etf_map
        info = em.health() if em is not None else {}

        def _row(label_text: str, value_text: str, value_color=None):
            row = tk.Frame(wrap, bg=c["BG"])
            row.pack(fill="x", anchor="w", pady=1)
            tk.Label(
                row, text=label_text, font=std_bold, width=22, anchor="w",
                bg=c["BG"], fg=c["FG"],
            ).pack(side="left")
            tk.Label(
                row, text=value_text, font=std, anchor="w",
                bg=c["BG"], fg=value_color or c["FG"], justify="left",
            ).pack(side="left")

        if info:
            refreshed = info.get("refreshed_at") or "(never)"
            stale_color = c["TXT_BAD"] if refreshed == "(never)" else c["FG"]
            _row("Last refreshed:", refreshed, stale_color)
            _row("Path:", info.get("path", "?"))
            _row(
                "File exists:",
                "yes" if info.get("exists") else "no",
                c["TXT_OK"] if info.get("exists") else c["TXT_BAD"],
            )
            _row(
                "Total underlyings:",
                str(info.get("total_underlyings", 0)),
            )
            _row("Total ETFs:", str(info.get("total_etfs", 0)))
            iss_text = ", ".join(
                f"{k} {v}" for k, v in sorted(
                    info.get("per_issuer", {}).items(),
                    key=lambda kv: -kv[1],
                )
            ) or "(none)"
            _row("Per issuer:", iss_text)
            top = info.get("top_underlyings", [])
            top_text = ", ".join(f"{k} ({n})" for k, n in top) or "(none)"
            _row("Top by ETF count:", top_text)
            issuers_run = ", ".join(info.get("issuers_scraped", [])) or "(none)"
            _row("Last scrape ran:", issuers_run)
            errs = info.get("errors", [])
            if errs:
                tk.Label(
                    wrap, text="Errors:", font=std_bold, anchor="w",
                    bg=c["BG"], fg=c["TXT_BAD"],
                ).pack(anchor="w", pady=(6, 0))
                for e in errs:
                    tk.Label(
                        wrap, text=f"  {e}", font=small, anchor="w",
                        bg=c["BG"], fg=c["TXT_BAD"], justify="left",
                        wraplength=560,
                    ).pack(anchor="w")

        tk.Button(
            wrap, text="Close", command=win.destroy, font=std, **btn_conf,
        ).pack(anchor="e", pady=(10, 0))
        win.bind("<Escape>", lambda _e: win.destroy())

    def _show_etf_popup(self, underlying, etfs, event=None):
        """Toplevel listing every ETF that tracks ``underlying``,
        sorted highest-leverage first. Each row clickable → opens
        Finviz for the ETF ticker."""
        c = self.colors
        win = tk.Toplevel(self)
        win.title(f"ETFs tracking {underlying}")
        win.configure(bg=c["BG"])
        win.transient(self)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        # Position near the indicator if we have the click event.
        try:
            if event is not None:
                x = self.winfo_rootx() + self.lbl_etf.winfo_x()
                y = (
                    self.winfo_rooty() + self.lbl_etf.winfo_y()
                    + self.lbl_etf.winfo_height() + 2
                )
                win.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass

        std = ("Segoe UI", self.base_font_size)
        std_bold = ("Segoe UI", self.base_font_size, "bold")
        small = ("Segoe UI", max(7, self.base_font_size - 1))

        wrap = tk.Frame(win, bg=c["BG"])
        wrap.pack(fill="both", expand=True, padx=10, pady=8)
        tk.Label(
            wrap, text=f"{underlying} — {len(etfs)} leveraged / inverse ETF(s)",
            font=std_bold, bg=c["BG"], fg=c["FG"],
        ).pack(anchor="w", pady=(0, 6))

        for e in etfs:
            row = tk.Frame(wrap, bg=c["BG"])
            row.pack(fill="x", anchor="w", pady=1)
            mult = e["mult"]
            mult_color = c["TXT_OK"] if mult > 0 else c["TXT_BAD"]
            tk.Label(
                row, text=e["ticker"], font=std_bold,
                bg=c["BG"], fg=c["ETF_BLUE"], cursor="hand2", width=8,
                anchor="w",
            ).pack(side="left")
            tk.Label(
                row, text=format_mult(mult), font=std,
                bg=c["BG"], fg=mult_color, width=6, anchor="w",
            ).pack(side="left", padx=(4, 8))
            tk.Label(
                row, text=f"({e.get('issuer', '')})", font=small,
                bg=c["BG"], fg=c["CREDIT"], anchor="w",
            ).pack(side="left")
            # Make the whole row clickable.
            def _open(_ev=None, t=e["ticker"]):
                safe = url_quote(str(t), safe="")
                self._safe_open_url(f"https://finviz.com/quote.ashx?t={safe}")
            for child in row.winfo_children():
                child.bind("<Button-1>", _open)
            row.bind("<Button-1>", _open)

        tk.Button(
            wrap, text="Close", command=win.destroy,
            bg=c["BTN_BG"], fg=c["BTN_FG"],
        ).pack(anchor="e", pady=(8, 0))
        win.bind("<Escape>", lambda _e: win.destroy())

    def _etf_popup_window(self, title, anchor_widget, event):
        """Shared Toplevel shell for the two ETF popups — themed, topmost,
        anchored under ``anchor_widget`` when we have a click event."""
        c = self.colors
        win = tk.Toplevel(self)
        win.title(title)
        win.configure(bg=c["BG"])
        win.transient(self)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        try:
            if event is not None and anchor_widget is not None:
                x = self.winfo_rootx() + anchor_widget.winfo_x()
                y = (self.winfo_rooty() + anchor_widget.winfo_y()
                     + anchor_widget.winfo_height() + 2)
                win.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass
        return win

    def _show_etf_holders_popup(self, stock, holders, event=None):
        """Toplevel listing every multi-holding ETF that holds ``stock``,
        leverage-first then by weight. Each row clickable → Finviz for the
        ETF. Mirrors the single-stock popup's organizational scheme."""
        c = self.colors
        win = self._etf_popup_window(f"ETFs holding {stock}",
                                     getattr(self, "lbl_etf_hold", None), event)
        std = ("Segoe UI", self.base_font_size)
        std_bold = ("Segoe UI", self.base_font_size, "bold")
        small = ("Segoe UI", max(7, self.base_font_size - 1))
        wrap = tk.Frame(win, bg=c["BG"])
        wrap.pack(fill="both", expand=True, padx=10, pady=8)
        tk.Label(
            wrap, text=f"{stock} — held by {len(holders)} multi-holding ETF(s)",
            font=std_bold, bg=c["BG"], fg=c["FG"],
        ).pack(anchor="w", pady=(0, 6))
        for h in holders:
            row = tk.Frame(wrap, bg=c["BG"])
            row.pack(fill="x", anchor="w", pady=1)
            mult = h.get("mult")
            mult_txt = format_mult(mult) if mult else "—"
            mult_color = c["CREDIT"] if not mult else (
                c["TXT_OK"] if mult > 0 else c["TXT_BAD"])
            tk.Label(row, text=h.get("etf", ""), font=std_bold,
                     bg=c["BG"], fg=c["ETF_BLUE"], cursor="hand2", width=8,
                     anchor="w").pack(side="left")
            tk.Label(row, text=mult_txt, font=std, bg=c["BG"], fg=mult_color,
                     width=6, anchor="w").pack(side="left", padx=(4, 8))
            try:
                wt = f"{float(h.get('weight') or 0):.1f}%"
            except (TypeError, ValueError):
                wt = ""
            tk.Label(row, text=wt, font=std, bg=c["BG"], fg=c["FG"],
                     width=7, anchor="w").pack(side="left")
            note = h.get("sector_label") or h.get("category") or ""
            if note:
                tk.Label(row, text=f"({note})", font=small, bg=c["BG"],
                         fg=c["CREDIT"], anchor="w").pack(side="left")

            def _open(_ev=None, t=h.get("etf", "")):
                safe = url_quote(str(t), safe="")
                self._safe_open_url(f"https://finviz.com/quote.ashx?t={safe}")
            for child in row.winfo_children():
                child.bind("<Button-1>", _open)
            row.bind("<Button-1>", _open)
        tk.Button(wrap, text="Close", command=win.destroy,
                  bg=c["BTN_BG"], fg=c["BTN_FG"]).pack(anchor="e", pady=(8, 0))
        win.bind("<Escape>", lambda _e: win.destroy())

    def _show_etf_constituents_popup(self, etf, prof, event=None):
        """Toplevel listing the ETF's constituents alphabetically. Each row
        clickable → Finviz for that holding."""
        c = self.colors
        win = self._etf_popup_window(f"{etf} holdings",
                                     getattr(self, "lbl_etf", None), event)
        std = ("Segoe UI", self.base_font_size)
        std_bold = ("Segoe UI", self.base_font_size, "bold")
        small = ("Segoe UI", max(7, self.base_font_size - 1))
        wrap = tk.Frame(win, bg=c["BG"])
        wrap.pack(fill="both", expand=True, padx=10, pady=8)
        holdings = sorted(prof.get("holdings") or [],
                          key=lambda h: h.get("ticker", ""))
        count = prof.get("count") or len(holdings)
        head = f"{etf}"
        if prof.get("sector_label"):
            head += f" · {prof['sector_label']}"
        if prof.get("mult"):
            head += f" · {format_mult(prof['mult']).upper()}"
        cap = (f"top {len(holdings)} of {count} holdings"
               if count > len(holdings) else f"{len(holdings)} holdings")
        if prof.get("date"):
            cap += f"  ({prof['date']})"
        tk.Label(wrap, text=head, font=std_bold, bg=c["BG"],
                 fg=c["ETF_BLUE"]).pack(anchor="w")
        tk.Label(wrap, text=cap, font=small, bg=c["BG"],
                 fg=c["CREDIT"]).pack(anchor="w", pady=(0, 6))
        for h in holdings:
            row = tk.Frame(wrap, bg=c["BG"])
            row.pack(fill="x", anchor="w", pady=1)
            tk.Label(row, text=h.get("ticker", ""), font=std_bold,
                     bg=c["BG"], fg=c["ETF_BLUE"], cursor="hand2", width=8,
                     anchor="w").pack(side="left")
            try:
                wt = f"{float(h.get('weight') or 0):.1f}%"
            except (TypeError, ValueError):
                wt = ""
            tk.Label(row, text=wt, font=std, bg=c["BG"], fg=c["FG"],
                     width=7, anchor="w").pack(side="left", padx=(4, 8))
            nm = h.get("name") or ""
            if nm:
                tk.Label(row, text=nm, font=small, bg=c["BG"], fg=c["CREDIT"],
                         anchor="w").pack(side="left")

            def _open(_ev=None, t=h.get("ticker", "")):
                safe = url_quote(str(t), safe="")
                self._safe_open_url(f"https://finviz.com/quote.ashx?t={safe}")
            for child in row.winfo_children():
                child.bind("<Button-1>", _open)
            row.bind("<Button-1>", _open)
        tk.Button(wrap, text="Close", command=win.destroy,
                  bg=c["BTN_BG"], fg=c["BTN_FG"]).pack(anchor="e", pady=(8, 0))
        win.bind("<Escape>", lambda _e: win.destroy())

    def on_double_click(self, event):
        """Open the URL of the row the user double-clicked.

        Doesn't trust the iid as an index — looks the row up by URL
        from the displayed values, which keeps working even if the
        iid scheme later changes (e.g. for the upcoming search filter
        when iids may diverge from positional indexes)."""
        # Prefer the row physically under the cursor over ``focus()``,
        # since selection state and the click target can disagree.
        try:
            iid = self.tree.identify_row(event.y) if event is not None else ""
        except tk.TclError:
            iid = ""
        if not iid:
            iid = self.tree.focus()
        if not iid:
            return

        # Historical mode: rows are keyed by ``hist_<idx>`` into the
        # results list. Skip non-data rows (banner, notes, loading).
        if self.historical_active:
            if not iid.startswith("hist_") or iid in (
                "hist_banner", "hist_loading",
            ):
                return
            try:
                idx = int(iid.split("_", 1)[1])
            except (ValueError, IndexError):
                return
            if 0 <= idx < len(self.historical_results):
                url = self.historical_results[idx].get("url")
                if url:
                    self._safe_open_url(url)
            return

        # Fast path: iid still maps to an index in current_items.
        item = None
        if iid.isdigit():
            i = int(iid)
            if 0 <= i < len(self.current_items):
                cand = self.current_items[i]
                # Validate by headline so a stale iid (after a list
                # reshuffle) can't open the wrong story.
                try:
                    row_vals = self.tree.item(iid, "values")
                except tk.TclError:
                    row_vals = ()
                if row_vals and len(row_vals) >= 3 and row_vals[2] == cand.get("headline"):
                    item = cand
        # Fallback: scan current_items by displayed headline+date.
        if item is None:
            try:
                row_vals = self.tree.item(iid, "values")
            except tk.TclError:
                return
            if not row_vals or len(row_vals) < 3:
                return
            row_date, _row_age, row_head = row_vals[0], row_vals[1], row_vals[2]
            for cand in self.current_items:
                if cand.get("headline") == row_head and cand.get("date") == row_date:
                    item = cand
                    break
        if item is None:
            return
        url = item.get("url")
        if url:
            self._safe_open_url(url)

    @staticmethod
    def _is_valid_hex_color(s):
        return bool(re.fullmatch(r"#[0-9A-Fa-f]{6}", s or ""))

    def load_settings(self):
        self._pending_show_earnings = False
        self._pending_show_48 = False
        self._pending_show_all = False
        self._pending_show_float = False
        self._pending_show_rvol = False
        self._pending_hot_words_new = ""
        self._pending_hot_words_old = ""
        self._pending_maximized = False
        self._pending_col_widths: dict = {}
        self._pending_finviz_min_interval: float = MIN_SCRAPE_INTERVAL
        # Float coloration — cutoff (shares) + the two colors. Colors
        # default to "" meaning "follow the theme's green/red", so the
        # baseline look is unchanged for users who never open Settings.
        self._pending_float_low_threshold: float = LOW_FLOAT_DEFAULT
        self._pending_float_low_color: str = ""
        self._pending_float_high_color: str = ""
        # Float coloration on/off (default on = prior low/high behavior).
        self._pending_float_color_enabled: bool = True
        # Market-cap stepped gradient: on/off (default on) + five tier
        # colors (default to the bright-red->bright-green ramp).
        self._pending_mcap_gradient_enabled: bool = True
        self._pending_mcap_tier_colors: dict = dict(MCAP_TIER_DEFAULT_COLORS)
        self._pending_search_visible: bool = False
        self._pending_search_kw: str = ""
        self._pending_search_date: str = ""
        # Earnings settings — both windows in days (0–60), and the
        # three colors used by ``refresh_meta_label``. Defaults match
        # the dark theme's existing values so the visual baseline is
        # unchanged for users who never open Settings.
        self._pending_earn_past_days: int = 9
        self._pending_earn_future_days: int = 9
        self._pending_earn_future_color: str = "#FFE600"
        self._pending_earn_pos_color: str = "#00D7FF"
        self._pending_earn_neg_color: str = "#FF4444"
        self._pending_earnings_db_path: str = DEFAULT_EARNINGS_DB_PATH
        self._pending_earnings_chart_font_mult: float = 1.0
        # Earnings chart window state — geometry string (size+pos like
        # "1000x940+120+60") and maximized flag. Persisted across
        # sessions so the chart reopens where the user left it.
        self._pending_earnings_chart_geometry: str = ""
        self._pending_earnings_chart_maximized: bool = False
        # Earnings-chart popup color overrides (user-editable via the
        # chart's "Colors…" button). Start from the defaults; the JSON
        # block below overrides any the user has saved.
        self._pending_chart_colors: dict = dict(self._chart_color_defaults())
        # Historical lookup tunables — EDGAR form list + Polygon
        # multi-ticker article cap.
        self._pending_historical_forms: str = DEFAULT_HISTORICAL_FORMS
        self._pending_historical_polygon_max_tickers: int = 5
        # SEC fair-access contact email (User-Agent). Empty = fall back to
        # the MS_SEC_CONTACT env var, then the non-deliverable placeholder.
        self._pending_sec_contact: str = ""
        if not SETTINGS_FILE.exists():
            return
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                # Valid JSON but not an object — treat as corrupt so the
                # ``data.get(...)`` calls below can't raise AttributeError
                # (not in this block's except tuple) and crash startup.
                raise ValueError(
                    f"settings root is {type(data).__name__}, not dict")
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            # Corrupt settings — preserve the bad copy so the user can
            # recover it and start fresh from defaults (R8). Without
            # this, on_close would silently overwrite the corrupt file
            # with the live state.
            try:
                bak = SETTINGS_FILE.with_suffix(".corrupt.bak")
                os.replace(SETTINGS_FILE, bak)
                _log.warning("settings file unreadable (%s); preserved as %s",
                              type(exc).__name__, bak.name)
            except OSError:
                pass
            return
        try:
            # Geometry: validate the shape before handing it to Tk. An
            # un-validated malformed value (e.g. from a partial write or a
            # hand-edit) makes self.geometry() raise tk.TclError, which is
            # NOT in this block's except tuple and would crash startup.
            geo = data.get("geometry")
            # Each position component is "<sign><number>" where the number
            # itself may be negative on a multi-monitor setup: a window on a
            # monitor to the LEFT/ABOVE the primary has a negative absolute
            # coordinate, which Tk's wm geometry reports as "+-1926" (a '+'
            # meaning "from the left edge" followed by a negative value). The
            # inner "-?" accepts that form — without it fullmatch rejected
            # every secondary-monitor geometry and the restore was silently
            # skipped (window always reopened at Tk's default placement).
            if isinstance(geo, str) and re.fullmatch(
                    r"\d+x\d+([+-]-?\d+){0,2}", geo):
                self.geometry(geo)
            self._pending_maximized = data.get("maximized", False)
            # font_size: accept only an int in the same 7..20 range the UI
            # clamps to. A non-numeric value (e.g. "big") would otherwise
            # crash startup via ``'big' += 0`` in adjust_font(0), outside
            # this try.
            fz = data.get("font_size")
            if isinstance(fz, int) and not isinstance(fz, bool) and 7 <= fz <= 20:
                self.base_font_size = fz
            self.theme_mode = data.get("theme", self.theme_mode)
            self._pending_show_earnings = data.get("show_earnings", False)
            self._pending_show_48 = data.get("show_48", False)
            self._pending_show_all = data.get("show_all", False)
            self._pending_show_float = data.get("show_float", False)
            self._pending_show_rvol = data.get("show_rvol", False)
            # Back-compat: old settings had one "hot_words" field.
            legacy = data.get("hot_words", "")
            self._pending_hot_words_new = data.get("hot_words_new", "")
            self._pending_hot_words_old = data.get("hot_words_old", legacy)
            saved_mode = data.get("watch_mode", "TS")
            if saved_mode in WATCH_MODES:
                self.watch_mode.set(saved_mode)
            cw = data.get("column_widths")
            if isinstance(cw, dict):
                self._pending_col_widths = {
                    k: int(v) for k, v in cw.items()
                    if isinstance(v, (int, float)) and v > 0
                }
            fz = data.get("finviz_min_interval")
            if isinstance(fz, (int, float)) and 0.1 <= fz <= 10.0:
                self._pending_finviz_min_interval = float(fz)
            # Float coloration: cutoff stored in raw shares; clamp to the
            # same range (in millions) the dialog enforces.
            flt = data.get("float_low_threshold")
            if isinstance(flt, (int, float)) and not isinstance(flt, bool):
                lo_m, hi_m = LOW_FLOAT_RANGE_M
                if lo_m * 1_000_000 <= flt <= hi_m * 1_000_000:
                    self._pending_float_low_threshold = float(flt)
            for key, attr in (("float_low_color", "_pending_float_low_color"),
                              ("float_high_color", "_pending_float_high_color")):
                v = data.get(key)
                # "" is valid (= follow theme); otherwise must be #RRGGBB.
                if isinstance(v, str) and (v == "" or self._is_valid_hex_color(v)):
                    setattr(self, attr, v)
            # Float coloration on/off.
            if isinstance(data.get("float_color_enabled"), bool):
                self._pending_float_color_enabled = data["float_color_enabled"]
            # Market-cap gradient on/off + per-tier colors.
            if isinstance(data.get("mcap_gradient_enabled"), bool):
                self._pending_mcap_gradient_enabled = data["mcap_gradient_enabled"]
            mtc = data.get("mcap_tier_colors")
            if isinstance(mtc, dict):
                for tier in MCAP_TIER_KEYS:
                    v = mtc.get(tier)
                    if isinstance(v, str) and self._is_valid_hex_color(v):
                        self._pending_mcap_tier_colors[tier] = v
            self._pending_search_visible = bool(data.get("search_visible", False))
            self._pending_search_kw = data.get("search_kw", "") or ""
            self._pending_search_date = data.get("search_date", "") or ""

            # Earnings settings — validate against the same ranges the
            # Settings dialog enforces so a hand-edited file can't put
            # the app in a broken state.
            ep = data.get("earn_past_days")
            if isinstance(ep, int) and 0 <= ep <= 60:
                self._pending_earn_past_days = ep
            ef = data.get("earn_future_days")
            if isinstance(ef, int) and 0 <= ef <= 60:
                self._pending_earn_future_days = ef
            for key, attr in (
                ("earn_future_color", "_pending_earn_future_color"),
                ("earn_pos_color", "_pending_earn_pos_color"),
                ("earn_neg_color", "_pending_earn_neg_color"),
            ):
                v = data.get(key)
                if isinstance(v, str) and self._is_valid_hex_color(v):
                    setattr(self, attr, v)
            # Earnings-chart popup colors. The value-label color may be
            # "" (= follow the date/fg color); the rest must be #RRGGBB.
            cc = self._pending_chart_colors
            for key, attr in self._CHART_COLOR_KEYS:
                v = data.get(key)
                if not isinstance(v, str):
                    continue
                if attr == "_chart_label_color":
                    if v == "" or self._is_valid_hex_color(v):
                        cc[attr] = v
                elif self._is_valid_hex_color(v):
                    cc[attr] = v
            edb = data.get("earnings_db_path")
            if isinstance(edb, str) and edb.strip():
                edb_clean = edb.strip()
                # One-shot migration: bump any of the known legacy
                # paths forward to the current default. Custom paths
                # are left alone.
                if edb_clean in _LEGACY_EARNINGS_DB_PATHS:
                    edb_clean = DEFAULT_EARNINGS_DB_PATH
                self._pending_earnings_db_path = edb_clean
            ecfm = data.get("earnings_chart_font_mult")
            if isinstance(ecfm, (int, float)) and 0.5 <= ecfm <= 2.5:
                self._pending_earnings_chart_font_mult = float(ecfm)
            ecg = data.get("earnings_chart_geometry")
            # Same "+-1926" secondary-monitor form as the main-window
            # geometry above — the inner "-?" keeps the chart popup's
            # restore from being skipped on a left/top monitor.
            if isinstance(ecg, str) and re.fullmatch(r"\d+x\d+([+-]-?\d+){0,2}", ecg or ""):
                self._pending_earnings_chart_geometry = ecg
            ecm = data.get("earnings_chart_maximized")
            if isinstance(ecm, bool):
                self._pending_earnings_chart_maximized = ecm
            hf = data.get("historical_forms")
            if isinstance(hf, str) and hf.strip():
                hf_clean = hf.strip()
                # One-shot migration: the pre-YoY-enrichment default
                # didn't include 10-K/10-Q in the EDGAR forms search,
                # so existing settings that match it exactly need to
                # bump forward to the new default to enable YoY rows.
                # Custom user forms strings are left alone.
                _LEGACY_HISTORICAL_FORMS = ("8-K,6-K,424B2,424B3,424B5,"
                                              "S-1,S-3,4,SC 13D,SC 13G,NT 10-K,NT 10-Q")
                if hf_clean == _LEGACY_HISTORICAL_FORMS:
                    hf_clean = DEFAULT_HISTORICAL_FORMS
                self._pending_historical_forms = hf_clean
            hmt = data.get("historical_polygon_max_tickers")
            if isinstance(hmt, int) and 0 <= hmt <= 100:
                self._pending_historical_polygon_max_tickers = hmt

            emp = data.get("etf_map_custom_path")
            if isinstance(emp, str):
                self._pending_etf_map_custom_path = emp.strip()

            sc = data.get("sec_contact")
            if isinstance(sc, str):
                self._pending_sec_contact = sc.strip()

            if self.theme_mode in THEMES:
                self.colors = THEMES[self.theme_mode]
        except (KeyError, ValueError, TypeError):
            pass

    def on_close(self):
        if self.watch_thread is not None:
            self.watch_thread.stop()
            # Briefly wait for the watcher to exit so its CoUninitialize
            # can't race an in-flight UIA call during teardown. Bounded;
            # daemon=True still guarantees exit if a call is wedged.
            self.watch_thread.join(timeout=1.0)
        self.fetcher.close()
        try:
            is_maximized = self.state() == "zoomed"
            if is_maximized:
                self.state("normal")
                self.update_idletasks()
            col_widths = {}
            for c in ("date", "age", "headline"):
                try:
                    col_widths[c] = int(self.tree.column(c, "width"))
                except tk.TclError:
                    pass
            data = {
                "geometry": self.geometry(),
                "maximized": is_maximized,
                "font_size": self.base_font_size,
                "theme": self.theme_mode,
                "show_earnings": self.var_earnings.get(),
                "show_48": self.var_48.get(),
                "show_all": self.var_all.get(),
                "show_float": self.var_float.get(),
                "show_rvol": self.var_rvol.get(),
                "hot_words_new": self.entry_hot_new.get(),
                "hot_words_old": self.entry_hot_old.get(),
                "watch_mode": self.watch_mode.get(),
                "column_widths": col_widths,
                "finviz_min_interval": float(self.fetcher.finviz_min_interval),
                "float_low_threshold": float(self.fetcher.float_low_threshold),
                "float_low_color": str(getattr(self, "float_low_color", "") or ""),
                "float_high_color": str(getattr(self, "float_high_color", "") or ""),
                "float_color_enabled": bool(getattr(self, "float_color_enabled", True)),
                "mcap_gradient_enabled": bool(getattr(self, "mcap_gradient_enabled", True)),
                "mcap_tier_colors": {
                    k: str(v) for k, v in
                    (getattr(self, "mcap_tier_colors", None) or
                     MCAP_TIER_DEFAULT_COLORS).items()
                },
                "search_visible": bool(self.search_visible.get()),
                "search_kw": self.entry_search_kw.get(),
                "search_date": self.entry_search_date.get(),
                "earn_past_days": int(self.earn_past_days),
                "earn_future_days": int(self.earn_future_days),
                "earn_future_color": str(self.earn_future_color),
                "earn_pos_color": str(self.earn_pos_color),
                "earn_neg_color": str(self.earn_neg_color),
                "earnings_db_path": str(self.earnings_db_path),
                "earnings_chart_font_mult": float(self.earnings_chart_font_mult),
                "earnings_chart_geometry": str(getattr(
                    self, "earnings_chart_geometry", "",
                ) or ""),
                "earnings_chart_maximized": bool(getattr(
                    self, "earnings_chart_maximized", False,
                )),
                "historical_forms": str(getattr(self, "historical_forms",
                                                 DEFAULT_HISTORICAL_FORMS)),
                "historical_polygon_max_tickers": int(getattr(
                    self, "historical_polygon_max_tickers", 5,
                )),
                "etf_map_custom_path": str(getattr(
                    self, "etf_map_custom_path", "",
                ) or ""),
                "sec_contact": str(getattr(self, "sec_contact", "") or ""),
                **{key: str(getattr(self, attr, ""))
                   for key, attr in self._CHART_COLOR_KEYS},
            }
            # Atomic write (temp + os.replace): a force-kill at shutdown —
            # exactly when an always-on-top app is most likely killed —
            # can never leave a truncated/empty settings file.
            _atomic_write_json(SETTINGS_FILE, data)
        except (OSError, TypeError):
            pass
        self.destroy()

    def _save_chart_colors(self):
        """Persist ONLY the earnings-chart popup colors immediately by
        merging them into the existing settings file. Lets a color edit
        survive even if the app is later killed before on_close runs;
        on_close still re-writes them with everything else. Best-effort —
        never raises into the UI."""
        try:
            data = {}
            if SETTINGS_FILE.exists():
                with open(SETTINGS_FILE, "r") as f:
                    data = json.load(f)
            if not isinstance(data, dict):
                # Valid JSON but not an object (e.g. ``[]``/``null``):
                # do NOT overwrite — that would silently destroy every
                # other persisted setting. Leave the anomalous file for
                # load_settings to quarantine on next launch.
                _log.warning("settings file is valid JSON but not a dict "
                             "(%s); skipping chart-color save to avoid "
                             "clobbering other settings", type(data).__name__)
                return
            for key, attr in self._CHART_COLOR_KEYS:
                data[key] = str(getattr(self, attr, ""))
            # Atomic merge-write so a mid-write crash can't truncate the
            # settings file (this runs on every valid color keystroke).
            _atomic_write_json(SETTINGS_FILE, data)
        except (OSError, ValueError, TypeError):
            pass

    def _merge_persist_settings(self, updates):
        """Best-effort: merge ``updates`` into the on-disk settings dict
        and atomically rewrite it, so a Settings-dialog Save survives a
        later force-kill without waiting for on_close (#18). Mirrors
        _save_chart_colors — never raises into the UI and refuses to
        clobber a valid-but-non-dict file."""
        try:
            data = {}
            if SETTINGS_FILE.exists():
                with open(SETTINGS_FILE, "r") as f:
                    data = json.load(f)
            if not isinstance(data, dict):
                _log.warning("settings file is valid JSON but not a dict "
                             "(%s); skipping settings merge-persist",
                             type(data).__name__)
                return
            data.update(updates)
            _atomic_write_json(SETTINGS_FILE, data)
        except (OSError, ValueError, TypeError):
            pass

if __name__ == "__main__":
    app = ScannerApp()
    app.mainloop()