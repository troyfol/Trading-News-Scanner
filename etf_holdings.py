"""Multi-holding ETF holdings map (stockanalysis.com-backed).

Companion to ``etf_map.py``. Where ``etf_map`` owns the *single-stock*
leveraged/inverse universe (one ETF tracks exactly one stock), this
module owns the *multi-holding* universe: ETFs — levered OR non-levered —
that hold a basket of more than one security (sector SPDRs, broad-index
funds, thematic/industry funds, and the leveraged index/sector funds).

Two access patterns mirror ``EtfMap``:

* **Self / forward** — ``get_profile(symbol)`` — given an ETF ticker like
  ``XLK`` returns its profile: leverage multiple (if any), a
  high-confidence sector/strategy label (else ``""``), the holding count,
  the source freshness date, and the top-N constituents. ``None`` when the
  symbol isn't a known multi-holding ETF.
* **Reverse** — ``get_holders_for(symbol)`` — given a stock like ``NVDA``
  returns the list of multi-holding ETFs that hold it (as a top-N
  constituent), as ``[{"etf", "mult", "weight", "category",
  "sector_label"}, ...]`` sorted leverage-first then by weight.

The reverse index is *derived* in memory by inverting every profile's
holdings list, so the on-disk file only stores profiles (single source of
truth, no inverted-data drift).

Path resolution, bundled-baseline seeding, and atomic writes follow the
exact same pattern as ``etf_map.py`` so the two behave identically from
the app's point of view.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

if getattr(sys, "frozen", False):
    _RUNTIME_DIR = Path(sys.executable).parent
    _BUNDLED_DIR = Path(getattr(sys, "_MEIPASS", _RUNTIME_DIR))
else:
    _RUNTIME_DIR = Path(__file__).parent
    _BUNDLED_DIR = _RUNTIME_DIR

DEFAULT_FILE_NAME = "etf_holdings.json"
DEFAULT_WRITABLE_PATH = _RUNTIME_DIR / DEFAULT_FILE_NAME
BUNDLED_BASELINE_PATH = _BUNDLED_DIR / DEFAULT_FILE_NAME

# See scan_sec._open_exclusive_temp for the full rationale. Local copy
# because scan_sec imports THIS module (importing it back would be a cycle).
_TEMP_NAME_ATTEMPTS = 5


def _open_exclusive_temp(path: Path, prefix: str = ".tmp_"):
    """Create a uniquely-named temp file in ``path``'s directory and return
    ``(fd, tmp_path)``. Bounded, unlike ``tempfile.mkstemp``, which on
    Windows loops ~2.1 billion times without raising when the directory is
    write-denied by ACL (``os.access(W_OK)`` ignores ACLs)."""
    for _ in range(_TEMP_NAME_ATTEMPTS):
        cand = path.parent / ("%s%s.json" % (prefix, os.urandom(8).hex()))
        try:
            fd = os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        return fd, cand
    raise OSError("could not create a unique temp file in %s" % path.parent)

# Minimum dominant-sector weight (percent) for a high-confidence sector
# prefix. Below this the holdings are too diversified (or swap-dominated,
# i.e. top sector == "Other") to label confidently, so the prefix is
# omitted per the product spec ("only if sector/strategy can be scraped
# with high confidence"). XLK (99.9% Tech) / JETS (89% Industrials) pass;
# SPY (38% Tech), ARKK (22% Tech), and leveraged funds (top == "Other")
# correctly do not.
SECTOR_PREFIX_MIN_WEIGHT = 60.0

# Sector names we never treat as a real, labelable sector.
_NON_SECTORS = {"other", "cash", "n/a", "na", "", "unknown", "-"}

# Long GICS-ish sector name -> short UI label for the "ETF: <label> <mult>"
# prefix. Anything not in the map falls through to the raw name.
_SECTOR_SHORT = {
    "technology": "Tech",
    "information technology": "Tech",
    "financial": "Financials",
    "financials": "Financials",
    "financial services": "Financials",
    "energy": "Energy",
    "health care": "Health",
    "healthcare": "Health",
    "health": "Health",
    "industrials": "Industrials",
    "consumer cyclical": "Consumer",
    "consumer discretionary": "Consumer",
    "consumer defensive": "Staples",
    "consumer staples": "Staples",
    "utilities": "Utilities",
    "basic materials": "Materials",
    "materials": "Materials",
    "real estate": "Real Estate",
    "communication services": "Comm",
    "communication": "Comm",
}


def short_sector(name: str) -> str:
    """Map a full sector name to its compact UI label (else the name)."""
    if not name:
        return ""
    return _SECTOR_SHORT.get(name.strip().lower(), name.strip())


def derive_sector_label(category: Optional[str],
                        sectors: Optional[list]) -> str:
    """High-confidence sector/strategy prefix for the ETF indicator.

    Returns a short label (e.g. ``"Tech"``) ONLY when a single real sector
    dominates the holdings at/above ``SECTOR_PREFIX_MIN_WEIGHT``. Returns
    ``""`` when the fund is diversified or swap-dominated (top sector is
    "Other"), so the caller shows a bare ``ETF:`` indicator. We deliberately
    do NOT infer a sector from the (often leverage-flavored) category string
    — that's the low-confidence path the spec asks us to skip.
    """
    if isinstance(sectors, list) and sectors:
        top = sectors[0]
        if isinstance(top, dict):
            name = str(top.get("n") or top.get("name") or "").strip()
            try:
                weight = float(top.get("w", top.get("weight", 0)) or 0)
            except (TypeError, ValueError):
                weight = 0.0
            if (name.lower() not in _NON_SECTORS
                    and weight >= SECTOR_PREFIX_MIN_WEIGHT):
                return short_sector(name)
    return ""


class EtfHoldings:
    """Thread-safe in-memory multi-holding ETF lookup, JSON-backed."""

    def __init__(self, path: Optional[Path] = None):
        self._lock = threading.RLock()
        self._path = Path(path) if path else DEFAULT_WRITABLE_PATH
        self._refreshed_at: Optional[str] = None
        self._source: str = ""
        self._errors: list[str] = []
        # Forward: {ETF: {mult, category, sector_label, count, date, holdings}}
        self._profiles: dict[str, dict] = {}
        # Reverse (derived): {STOCK: [{etf, mult, weight, category, sector_label}]}
        self._holders: dict[str, list[dict]] = {}
        self._seed_writable_from_bundle()
        self.reload()

    # ---- public API ------------------------------------------------------

    @property
    def path(self) -> Path:
        with self._lock:
            return self._path

    def set_path(self, new_path: Path) -> None:
        new_path = Path(new_path) if new_path else DEFAULT_WRITABLE_PATH
        with self._lock:
            if new_path == self._path:
                return
            self._path = new_path
            self.reload()

    def reload(self) -> None:
        with self._lock:
            data = self._read_json(self._path)
            if data is None and self._path != BUNDLED_BASELINE_PATH:
                logger.warning(
                    "ETF holdings: custom path %s unreadable; "
                    "falling back to bundled baseline", self._path,
                )
                data = self._read_json(BUNDLED_BASELINE_PATH)
            self._apply(data or {})

    def get_profile(self, symbol: str) -> Optional[dict]:
        """Return the ETF's profile dict if ``symbol`` IS a known
        multi-holding ETF, else ``None``."""
        if not symbol:
            return None
        with self._lock:
            p = self._profiles.get(symbol.upper().strip())
            if not p:
                return None
            # Deep-ish copy so callers can't mutate our state.
            out = dict(p)
            out["holdings"] = [dict(h) for h in p.get("holdings", [])]
            return out

    def is_etf(self, symbol: str) -> bool:
        if not symbol:
            return False
        with self._lock:
            return symbol.upper().strip() in self._profiles

    def get_holders_for(self, symbol: str) -> list[dict]:
        """Return the multi-holding ETFs that hold ``symbol`` (as a top-N
        constituent), sorted leverage-first then by weight desc."""
        if not symbol:
            return []
        with self._lock:
            return [dict(r) for r in self._holders.get(symbol.upper().strip(), [])]

    def health(self) -> dict:
        with self._lock:
            levered = sum(1 for p in self._profiles.values() if p.get("mult"))
            return {
                "path": str(self._path),
                "exists": self._path.exists(),
                "refreshed_at": self._refreshed_at,
                "source": self._source,
                "errors": list(self._errors),
                "total_etfs": len(self._profiles),
                "levered_etfs": levered,
                "total_stocks_covered": len(self._holders),
            }

    def replace(self, profiles: dict, *, source: str = "",
                errors: Optional[list] = None) -> None:
        """Atomically replace the profiles map (called by the scraper) and
        persist. Refuses to overwrite good data with an empty scrape that
        also reported errors (same floor as EtfMap.replace)."""
        normalized = self._normalize_profiles(profiles)
        errors = list(errors or [])
        payload = {
            "schema_version": 1,
            "refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": source,
            "errors": errors,
            "profiles": normalized,
        }
        with self._lock:
            if not normalized and errors:
                return
            self._backup_existing(self._path)
            self._write_atomic(self._path, payload)
            self._apply(payload)

    def snapshot_profiles(self) -> dict:
        """Per-entry copy of the whole profiles map (for preserve-on-failure
        in the refresh worker)."""
        with self._lock:
            out = {}
            for k, v in self._profiles.items():
                cp = dict(v)
                cp["holdings"] = [dict(h) for h in v.get("holdings", [])]
                out[k] = cp
            return out

    # ---- internals --------------------------------------------------------

    @staticmethod
    def _backup_existing(path: Path) -> None:
        try:
            if path.exists():
                shutil.copy2(path, path.with_name(path.name + ".bak"))
        except OSError:
            pass

    def _seed_writable_from_bundle(self) -> None:
        try:
            if (
                self._path == DEFAULT_WRITABLE_PATH
                and not self._path.exists()
                and BUNDLED_BASELINE_PATH.exists()
                and BUNDLED_BASELINE_PATH != self._path
            ):
                self._path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(BUNDLED_BASELINE_PATH, self._path)
        except OSError:
            pass

    @staticmethod
    def _read_json(path: Path) -> Optional[dict]:
        try:
            if not path.exists():
                return None
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            return data
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _write_atomic(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # See EtfMap._write_atomic / scan_sec._open_exclusive_temp: mkstemp
        # spins forever instead of raising in an ACL-write-denied directory
        # on Windows, because os.access(W_OK) ignores ACLs.
        fd, tmp = _open_exclusive_temp(path, prefix=".etfhold_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _normalize_profiles(profiles: dict) -> dict:
        """Clean each profile: uppercase the ETF key + holding tickers,
        coerce types, drop malformed entries and self-referential or
        single-holding funds (those belong to the single-stock map)."""
        out: dict[str, dict] = {}
        for raw_etf, prof in (profiles or {}).items():
            if not isinstance(raw_etf, str) or not isinstance(prof, dict):
                continue
            etf = raw_etf.upper().strip()
            if not etf:
                continue
            raw_holdings = prof.get("holdings")
            if not isinstance(raw_holdings, list):
                continue
            holdings = []
            seen_tk: set = set()
            for h in raw_holdings:
                if not isinstance(h, dict):
                    continue
                tk = str(h.get("ticker") or "").upper().strip()
                # Skip blank, self-reference, and non-equity rows.
                if not tk or tk == etf or tk in seen_tk:
                    continue
                try:
                    w = float(h.get("weight"))
                except (TypeError, ValueError):
                    w = 0.0
                seen_tk.add(tk)
                holdings.append({
                    "ticker": tk,
                    "name": str(h.get("name") or "").strip(),
                    "weight": w,
                })
            mult = prof.get("mult")
            try:
                mult = float(mult) if mult is not None else None
            except (TypeError, ValueError):
                mult = None
            if mult is not None and (mult != mult or mult == 0
                                     or abs(mult) > 5.0):
                mult = None
            # A "multi-holding" ETF normally holds >1 distinct security. A
            # 0/1-holding result is usually a swap-only fund (leveraged /
            # inverse funds hold total-return swaps, which the source
            # reports without share tickers) or a mis-scrape. KEEP it only
            # when it's a known leveraged fund (mult set) so the ETF-self
            # indicator can still show a blue "ETF: <mult>X" badge — the
            # empty holdings list means it contributes nothing to the
            # reverse map. Non-leveraged funds with <2 holdings are dropped
            # as mis-scrapes (a real basket fund always lists its shares).
            if len(holdings) < 2 and mult is None:
                continue
            try:
                count = int(prof.get("count") or len(holdings))
            except (TypeError, ValueError):
                count = len(holdings)
            out[etf] = {
                "mult": mult,
                "category": str(prof.get("category") or "").strip(),
                "sector_label": str(prof.get("sector_label") or "").strip(),
                "count": count,
                "date": str(prof.get("date") or "").strip(),
                "holdings": holdings,
            }
        return out

    def _apply(self, payload: dict) -> None:
        profiles_raw = payload.get("profiles") or {}
        self._profiles = self._normalize_profiles(profiles_raw)
        # Build the reverse index by inverting every profile's holdings.
        holders: dict[str, list[dict]] = {}
        for etf, prof in self._profiles.items():
            # Inverse funds SHORT their basket — a stock they reference is
            # not "held". Keep them out of the reverse (stock -> ETFs) map
            # so "Held: N" only ever means LONG exposure. They still get a
            # self-view profile + blue badge above.
            if (prof.get("mult") or 0) < 0:
                continue
            for h in prof.get("holdings", []):
                holders.setdefault(h["ticker"], []).append({
                    "etf": etf,
                    "mult": prof.get("mult"),
                    "weight": h.get("weight", 0.0),
                    "category": prof.get("category", ""),
                    "sector_label": prof.get("sector_label", ""),
                })
        # Sort each stock's holder list: leverage-first (by abs mult desc,
        # bull before bear), then non-levered by the stock's weight in the
        # fund desc, then ticker — mirrors the single-stock map's scheme.
        for tk, rows in holders.items():
            rows.sort(key=lambda r: (
                0 if r.get("mult") else 1,
                -abs(r.get("mult") or 0.0),
                -(r.get("mult") or 0.0),
                -(r.get("weight") or 0.0),
                r.get("etf", ""),
            ))
        self._holders = holders
        self._refreshed_at = payload.get("refreshed_at")
        self._source = str(payload.get("source") or "")
        errs = payload.get("errors") or []
        self._errors = [str(x) for x in errs if isinstance(x, str)]
